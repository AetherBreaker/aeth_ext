"""Per-session transfer logic shared by `AdaptedFTP`/`AdaptedSFTP`. Holds only the acquire/release/handler
plumbing -- every transfer-protocol method (upload_file, download_file, transfer_file, ...) is defined
directly on `AdaptedFTP`/`AdaptedSFTP` themselves, never on a shared mixin, so goto-definition on a
transfer call always lands on the real implementation.
"""

# Standard library imports
from abc import ABC, abstractmethod
from contextlib import nullcontext
from datetime import datetime
from ftplib import FTP, _SSLSocket, all_errors  # type: ignore
from io import BytesIO
from logging import getLogger
from typing import TYPE_CHECKING, override

# Third party imports
from paramiko import SFTPClient, SFTPError, SSHException

# First party imports
from aeth_ext.ftp.types import (
  HandleProvider,
  InstrumentCallable,
  ListDirResult,
  ReadCallback,
  TransferSuccess,
  WriteCallback,
)
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator, Sequence
  from types import TracebackType
  from typing import Self
  from zoneinfo import ZoneInfo

  # First party imports
  from aeth_ext.rich.progress import Progress
  from aeth_ext.types import SizedBuffer


logger = getLogger(__name__)

SETTINGS = BaseSettings.get_settings()


__all__ = ["AdaptedFTP", "AdaptedSFTP"]


_CONNECTION_FATAL_TYPES = (TimeoutError, ConnectionError, BrokenPipeError, EOFError, SSHException)


class _AdaptedSessionBase[HandleT]:
  __slots__ = ("_callbacks", "_ctor_callbacks", "_entries", "_provider", "chunk_size", "container_cls", "handler", "pbar", "tzinfo")

  def __init__(
    self,
    provider: HandleProvider[HandleT],
    *,
    container_cls: str | None,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    chunk_size: int = 8192,
    callbacks: Sequence[InstrumentCallable] = (),
  ) -> None:
    """Stores session configuration; does not acquire a handle until `__enter__`.

    Args:
      provider: Supplies (and reclaims) the connection handle this session uses.
      container_cls: Label attached to log messages this session emits.
      pbar: Progress reporter for transfer methods to report against, if any.
      tzinfo: Timezone used to localize server-reported modification times.
      chunk_size: Bytes read/written per I/O call by transfer methods.
      callbacks: Observers invoked with each chunk transferred, in addition to any the provider
        attaches at acquire time.
    """
    self.handler: HandleT | None = None
    self._entries = 0
    self._provider = provider
    # Kept apart from _callbacks so re-entry can rebuild the combined tuple from a clean base:
    # provider-supplied observers are scoped to one checkout, these live as long as the session.
    self._ctor_callbacks = tuple(callbacks)
    self._callbacks = self._ctor_callbacks
    self.container_cls = container_cls
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.chunk_size = chunk_size
    super().__init__()

  def __enter__(self) -> Self:
    """Acquires a handle from `provider` on first entry; nested re-entry only bumps a depth counter.

    Returns:
      This session.
    """
    if self._entries == 0:
      self.handler, acquired = self._provider.acquire()
      # Rebuilt from _ctor_callbacks rather than appended to _callbacks: the provider hands out
      # observers bound to the handle being checked out (for SFTP, to its Transport's throughput
      # state), so appending would keep every previous checkout's observers alive across re-entry --
      # reporting each later chunk to stale state, once more per reuse.
      self._callbacks = (*self._ctor_callbacks, *acquired)
    self._entries += 1
    return self

  def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
    """Releases the handle once the outermost nested `with` exits; marks it fatal if `exc_val`
    indicates a broken connection.

    Args:
      exc_type: Unused; part of the context-manager protocol.
      exc_val: The exception raised inside the `with` block, if any.
      exc_tb: Unused; part of the context-manager protocol.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert self._entries > 0, "__exit__ called without a matching __enter__"
    self._entries -= 1
    if self._entries > 0:
      # An inner exit of a nested `with self: ...` block -- the outer block still owns the handle.
      return
    self._provider.release(self.handler, isinstance(exc_val, _CONNECTION_FATAL_TYPES))
    self.handler = None
    self._callbacks = self._ctor_callbacks  # drop this checkout's provider observers; keep the session's own

  def _notify(self, data: SizedBuffer) -> None:
    """Invokes every constructor/acquire-time observer with one transferred chunk."""
    for observer in self._callbacks:
      observer(data)


class AdapterBase(ABC):
  __slots__ = ()

  @abstractmethod
  def test_connection(self, logit: bool = False) -> bool:
    """Tests the connection to the FTP/SFTP server.

    Args:
      logit: Whether to log the failure reason if the connection test fails.

    Returns:
      `True` if the connection succeeded, `False` otherwise.
    """
    raise NotImplementedError

  @abstractmethod
  def get_size(self, path: str) -> int | None:
    """Returns a file's size.

    Args:
      path: Absolute path to a file on the server.

    Returns:
      The file's size in bytes, or `None` if it can't be determined.
    """
    raise NotImplementedError

  @abstractmethod
  def upload_file(self, remote_path: str, callback: ReadCallback, file_size: int, task_msg: str = "") -> None:
    """Uploads a file by pulling its bytes from `callback` until it returns an empty chunk.

    Args:
      remote_path: Absolute destination path on the server.
      callback: Called with the desired chunk size; returns that many bytes, or `b""` when done.
      file_size: Total size of the upload, for progress reporting.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    raise NotImplementedError

  @abstractmethod
  def download_file(self, remote_path: str, callback: WriteCallback, task_msg: str = "") -> None:
    """Downloads a file, pushing each chunk read to `callback`.

    Args:
      remote_path: Absolute source path on the server.
      callback: Called with each chunk read.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    raise NotImplementedError

  @abstractmethod
  def transfer_file(  # noqa: PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP | AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Transfers a file server-to-server, without saving it to local disk.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination session (may be FTP or SFTP).
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
    raise NotImplementedError

  @abstractmethod
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the server.

    Args:
      old_remote_path: Absolute current path.
      new_remote_path: Absolute path to rename it to.
    """
    raise NotImplementedError

  @abstractmethod
  def remove(self, remote_path: str) -> None:
    """Deletes a file on the server.

    Args:
      remote_path: Absolute path to the file to delete.
    """
    raise NotImplementedError

  @abstractmethod
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Lists entries (bare filenames, not full paths) in a directory.

    An entry whose modification time can't be determined is skipped, not yielded with a `None` time.

    Args:
      path: Absolute path to a directory on the server.
    """
    raise NotImplementedError

  @abstractmethod
  def makedir(self, remote_path: str) -> None:
    """Creates a directory on the server.

    Args:
      remote_path: Absolute path of the directory to create.
    """
    raise NotImplementedError


class AdaptedFTP(_AdaptedSessionBase[FTP], AdapterBase):
  __slots__ = ()

  @override
  def upload_file(self, remote_path: str, callback: ReadCallback, file_size: int, task_msg: str = "") -> None:
    """Streams `callback`-supplied chunks to `remote_path` over the FTP data connection until it
    returns an empty chunk.

    Args:
      remote_path: Absolute destination path on the server.
      callback: Called with `self.chunk_size`; returns that many bytes, or `b""` when done.
      file_size: Total upload size, for progress reporting.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      self.handler.voidcmd("TYPE I")  # Set binary mode
      with self.handler.transfercmd(f"STOR {remote_path}") as conn:
        with (
          self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=file_size)
          if self.pbar is not None
          else nullcontext() as transfer_task
        ):
          while buffer := callback(self.chunk_size):
            conn.sendall(buffer)
            self._notify(buffer)
            if self.pbar is not None:
              assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
              self.pbar.update(transfer_task, advance=len(buffer))
        if _SSLSocket is not None and isinstance(conn, _SSLSocket):
          conn.unwrap()  # type: ignore
    finally:
      self.handler.voidresp()

  @override
  def download_file(self, remote_path: str, callback: WriteCallback, task_msg: str = "") -> None:
    """Streams `remote_path`'s contents from the FTP data connection to `callback`, chunk by chunk.

    Args:
      remote_path: Absolute source path on the server.
      callback: Called with each chunk read.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      self.handler.voidcmd("TYPE I")  # Set binary mode
      socket, size = self.handler.ntransfercmd(f"RETR {remote_path}")
      if size is None:
        size = self.handler.size(remote_path)
      with socket as conn:
        with (
          self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=size)
          if self.pbar is not None
          else nullcontext() as transfer_task
        ):
          while data := conn.recv(self.chunk_size):
            callback(data)
            self._notify(data)
            if self.pbar is not None:
              assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
              self.pbar.update(transfer_task, advance=len(data))
        if _SSLSocket is not None and isinstance(conn, _SSLSocket):
          conn.unwrap()  # type: ignore
    finally:
      self.handler.voidresp()

  @override
  def transfer_file(
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP | AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Dispatches to the FTP-to-FTP or FTP-to-SFTP transfer path based on `other`'s type.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
    if isinstance(other, AdaptedFTP):
      return self._ftp_to_ftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    elif isinstance(other, AdaptedSFTP):  # pyright: ignore[reportUnnecessaryIsInstance]
      return self._ftp_to_sftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    else:
      raise TypeError(f"Unsupported other protocol: {other.__class__}")  # pyright: ignore[reportUnreachable]

  def _ftp_to_sftp(  # noqa: PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Streams `source_remote_path` from this FTP connection directly into `other`'s SFTP connection.

    Args:
      source_remote_path: Absolute source path on this session's FTP server.
      dest_remote_path: Absolute destination path on `other`'s SFTP server.
      other: The destination SFTP session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to both sessions' own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert other.handler is not None, "Other adapter must also be opened as a context manager"
    conn, source_file_size = self.handler.ntransfercmd(f"RETR {source_remote_path}")
    if source_file_size is None:
      try:
        source_file_size = self.handler.size(source_remote_path)
      except all_errors as e:
        logger.exception("%s: Failed to get source file size for %s", self.container_cls, source_remote_path, exc_info=e)
        source_file_size = None
    mem_stream = mem_stream or BytesIO()
    with (
      other.handler.open(dest_remote_path, mode="wb") as dest_file,
    ):
      with (
        self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
        if self.pbar is not None
        else nullcontext() as transfer_task
      ):
        with conn as source_conn:
          while data := source_conn.recv(self.chunk_size):
            if callback is not None:
              callback(data)
            self._notify(data)
            dest_file.write(data)
            other._notify(data)  # destination-side SFTP instrumentation, after the write it measures
            mem_stream.write(data)
            if self.pbar is not None:
              assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
              self.pbar.update(transfer_task, advance=len(data))
          if _SSLSocket is not None and isinstance(source_conn, _SSLSocket):
            source_conn.unwrap()  # type: ignore
        self.handler.voidresp()

      streamed_file_size = mem_stream.tell()
      try:
        dest_file_size = dest_file.tell()
      except Exception as e:
        dest_file_size = None
        logger.exception("%s: Failed to get destination file size after transfer", self.container_cls, exc_info=e)
        return False
    # all three file sizes should be equal
    result = (
      source_file_size == streamed_file_size == dest_file_size
      if source_file_size is not None
      else streamed_file_size == dest_file_size
    )
    if not result:
      logger.error(
        "%s: File size mismatch after transfer: source_file_size=%s, streamed_file_size=%s, dest_file_size=%s",
        self.container_cls,
        source_file_size,
        streamed_file_size,
        dest_file_size,
      )
    return result

  def _ftp_to_ftp(  # noqa: C901, PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Streams `source_remote_path` from this FTP connection directly into `other`'s FTP connection.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination FTP session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert other.handler is not None, "Other adapter must also be opened as a context manager"
    self.handler.voidcmd("TYPE I")  # Set binary mode
    socket, source_file_size = self.handler.ntransfercmd(f"RETR {source_remote_path}")
    if source_file_size is None:
      try:
        source_file_size = self.handler.size(source_remote_path)
      except all_errors:
        source_file_size = None
        logger.exception("%s: Failed to get source file size.", self.container_cls)
    mem_stream = mem_stream or BytesIO()
    with (
      self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
      if self.pbar is not None
      else nullcontext() as transfer_task
    ):
      self.handler.voidcmd("TYPE I")  # Set binary mode
      other.handler.voidcmd("TYPE I")  # Set binary mode
      with (
        socket as source_conn,
        other.handler.transfercmd(f"STOR {dest_remote_path}") as dest_conn,
      ):
        while data := source_conn.recv(self.chunk_size):
          if callback is not None:
            callback(data)
          self._notify(data)
          dest_conn.sendall(data)
          other._notify(data)  # destination-side observers, after the send it measures
          mem_stream.write(data)
          if self.pbar is not None:
            assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
            self.pbar.update(transfer_task, advance=len(data))
        if _SSLSocket is not None:
          if isinstance(source_conn, _SSLSocket):
            source_conn.unwrap()  # type: ignore
          if isinstance(dest_conn, _SSLSocket):
            dest_conn.unwrap()  # type: ignore
      self.handler.voidresp()
      other.handler.voidresp()
    streamed_file_size = mem_stream.tell()
    try:
      dest_file_size = other.handler.size(dest_remote_path)
    except all_errors:
      dest_file_size = None
      logger.exception("%s: Failed to get destination file size after transfer.", self.container_cls)
      return False
    # all three file sizes should be equal
    result = (
      source_file_size == streamed_file_size == dest_file_size
      if source_file_size is not None
      else streamed_file_size == dest_file_size
    )
    if not result:
      logger.error(
        "%s: File size mismatch after transfer: source_file_size=%s, streamed_file_size=%s, dest_file_size=%s",
        self.container_cls,
        source_file_size,
        streamed_file_size,
        dest_file_size,
      )
    return result

  @override
  def get_size(self, path: str) -> int | None:
    """Returns a file's size.

    Args:
      path: Absolute path to a file on the server.

    Returns:
      The file's size in bytes.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.voidcmd("TYPE I")  # Set binary mode
    return self.handler.size(path)

  @override
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the server.

    Args:
      old_remote_path: Absolute current path.
      new_remote_path: Absolute path to rename it to.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.rename(old_remote_path, new_remote_path)

  @override
  def remove(self, remote_path: str) -> None:
    """Deletes a file on the server.

    Args:
      remote_path: Absolute path to the file to delete.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.delete(remote_path)

  @override
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Lists entries via `MLSD`, skipping any entry with no `modify` fact.

    Args:
      path: Absolute path to a directory on the server.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    for entry in self.handler.mlsd(path):
      name, facts = entry
      if "modify" in facts:
        dt = datetime.strptime(facts["modify"], "%Y%m%d%H%M%S")  # noqa: DTZ007
        new_dt = dt.replace(tzinfo=self.tzinfo)
        yield ListDirResult(filename=name, modified_time=new_dt)

  @override
  def test_connection(self, logit: bool = False) -> bool:
    """Tests the connection with a `NOOP` round trip.

    Args:
      logit: Whether to log the failure reason if the connection test fails.

    Returns:
      `True` if the round trip succeeded, `False` otherwise.
    """
    try:
      with self as ftp:
        assert isinstance(ftp.handler, FTP)
        ftp.handler.voidcmd("NOOP")
      return True
    except Exception:
      if logit:
        logger.exception("%s: Waiting FTP server is offline", self.container_cls)
      return False

  @override
  def makedir(self, remote_path: str) -> None:
    """Creates a directory on the server.

    Args:
      remote_path: Absolute path of the directory to create.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.mkd(remote_path)


class AdaptedSFTP(_AdaptedSessionBase[SFTPClient], AdapterBase):
  __slots__ = ()

  @override
  def upload_file(self, remote_path: str, callback: ReadCallback, file_size: int, task_msg: str = "") -> None:
    """Streams `callback`-supplied chunks to `remote_path` until it returns an empty chunk.

    Args:
      remote_path: Absolute destination path on the server.
      callback: Called with `self.chunk_size`; returns that many bytes, or `b""` when done.
      file_size: Total upload size, for progress reporting.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    with (
      self.handler.open(remote_path, mode="wb") as remote_file,
      (
        self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=file_size) if self.pbar is not None else nullcontext()
      ) as transfer_task,
    ):
      while buffer := callback(self.chunk_size):
        remote_file.write(buffer)
        self._notify(buffer)
        if self.pbar is not None:
          assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
          self.pbar.update(transfer_task, advance=len(buffer))

  @override
  def download_file(self, remote_path: str, callback: WriteCallback, task_msg: str = "") -> None:
    """Streams `remote_path`'s contents to `callback`, chunk by chunk.

    Prefetches the whole file up front so paramiko can pipeline reads instead of waiting on each
    request/response round trip.

    Args:
      remote_path: Absolute source path on the server.
      callback: Called with each chunk read.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    with self.handler.open(remote_path, mode="rb") as remote_file:
      size = remote_file.stat().st_size
      remote_file.prefetch(size)
      with (
        self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=size)
        if self.pbar is not None
        else nullcontext() as transfer_task
      ):
        while data := remote_file.read(self.chunk_size):
          callback(data)
          self._notify(data)
          if self.pbar is not None:
            assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
            self.pbar.update(transfer_task, advance=len(data))

  @override
  def transfer_file(
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedSFTP | AdaptedFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Dispatches to the SFTP-to-FTP or SFTP-to-SFTP transfer path based on `other`'s type.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
    if isinstance(other, AdaptedFTP):
      return self._sftp_to_ftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    elif isinstance(other, AdaptedSFTP):  # pyright: ignore[reportUnnecessaryIsInstance]
      return self._sftp_to_sftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    else:
      raise TypeError(f"Unsupported protocol kind: {other.__class__}")  # pyright: ignore[reportUnreachable]

  def _sftp_to_ftp(  # noqa: PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Streams `source_remote_path` from this SFTP connection directly into `other`'s FTP connection.

    Args:
      source_remote_path: Absolute source path on this session's SFTP server.
      dest_remote_path: Absolute destination path on `other`'s FTP server.
      other: The destination FTP session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert other.handler is not None, "Other adapter must also be opened as a context manager"
    try:
      source_file_size = self.handler.stat(source_remote_path).st_size
    except SFTPError:
      source_file_size = None
      logger.exception("%s: Failed to get source file size for %s.", self.container_cls, source_remote_path)
    mem_stream = mem_stream or BytesIO()
    with (
      self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
      if self.pbar is not None
      else nullcontext() as transfer_task
    ):
      other.handler.voidcmd("TYPE I")  # Set binary mode
      with (
        other.handler.transfercmd(f"STOR {dest_remote_path}") as dest_conn,
        self.handler.open(source_remote_path, mode="rb") as source_file,
      ):
        while data := source_file.read(self.chunk_size):
          if callback is not None:
            callback(data)
          self._notify(data)
          dest_conn.sendall(data)
          other._notify(data)  # destination-side observers, after the send it measures
          mem_stream.write(data)
          if self.pbar is not None:
            assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
            self.pbar.update(transfer_task, advance=len(data))
        if _SSLSocket is not None and isinstance(dest_conn, _SSLSocket):
          dest_conn.unwrap()  # type: ignore
      other.handler.voidresp()
    streamed_file_size = mem_stream.tell()
    try:
      dest_file_size = other.handler.size(dest_remote_path)
    except all_errors:
      dest_file_size = None
      logger.exception("%s: Failed to get destination file size after transfer.", self.container_cls)
      return False
    # all three file sizes should be equal
    result = (
      source_file_size == streamed_file_size == dest_file_size
      if source_file_size is not None
      else streamed_file_size == dest_file_size
    )
    if not result:
      logger.error(
        "%s: File size mismatch after transfer: source_file_size=%s, streamed_file_size=%s, dest_file_size=%s",
        self.container_cls,
        source_file_size,
        streamed_file_size,
        dest_file_size,
      )
    return result

  def _sftp_to_sftp(  # noqa: PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Streams `source_remote_path` from this SFTP connection directly into `other`'s SFTP connection.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination SFTP session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to both sessions' own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert other.handler is not None, "Other adapter must also be opened as a context manager"
    try:
      source_file_size = self.handler.stat(source_remote_path).st_size
    except SFTPError:
      source_file_size = None
      logger.exception("%s: Failed to get source file size for %s.", self.container_cls, source_remote_path)
    mem_stream = mem_stream or BytesIO()
    with other.handler.open(dest_remote_path, mode="wb") as dest_file:
      with (
        (
          self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
          if self.pbar is not None
          else nullcontext()
        ) as transfer_task,
        self.handler.open(source_remote_path, mode="rb") as source_file,
      ):
        while data := source_file.read(self.chunk_size):
          if callback is not None:
            callback(data)
          self._notify(data)
          dest_file.write(data)
          other._notify(data)  # destination-side SFTP instrumentation, after the write it measures
          mem_stream.write(data)
          if self.pbar is not None:
            assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
            self.pbar.update(transfer_task, advance=len(data))
      streamed_file_size = mem_stream.tell()
      try:
        dest_file_size = dest_file.tell()
      except Exception:
        dest_file_size = None
        logger.exception("%s: Failed to get destination file size after transfer.", self.container_cls)
        return False
    # all three file sizes should be equal
    result = (
      source_file_size == dest_file_size == streamed_file_size
      if source_file_size is not None
      else streamed_file_size == dest_file_size
    )
    if not result:
      logger.error(
        "%s: File size mismatch after transfer: source_file_size=%s, dest_file_size=%s, streamed_file_size=%s",
        self.container_cls,
        source_file_size,
        dest_file_size,
        streamed_file_size,
      )
    return result

  @override
  def get_size(self, path: str) -> int | None:
    """Returns a file's size.

    Args:
      path: Absolute path to a file on the server.

    Returns:
      The file's size in bytes, or `None` on `SFTPError`.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      return self.handler.stat(path).st_size
    except SFTPError:
      logger.exception("%s: Failed to get file size for %s.", self.container_cls, path)
      return None

  @override
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the server.

    Args:
      old_remote_path: Absolute current path.
      new_remote_path: Absolute path to rename it to.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.rename(old_remote_path, new_remote_path)

  @override
  def remove(self, remote_path: str) -> None:
    """Deletes a file on the server.

    Args:
      remote_path: Absolute path to the file to delete.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.remove(remote_path)

  @override
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Lists entries in a directory.

    Args:
      path: Absolute path to a directory on the server.

    Raises:
      ValueError: An entry has no modification time -- unlike `AdaptedFTP.listdir`, this does not
        silently skip it.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    for entry in self.handler.listdir_iter(path):
      if entry.st_mtime is None:
        raise ValueError(f"Entry {entry.filename} does not have a modification time, cannot be used in _sftp_listdir")
      yield ListDirResult(filename=entry.filename, modified_time=datetime.fromtimestamp(entry.st_mtime, tz=self.tzinfo))

  @override
  def test_connection(self, logit: bool = False) -> bool:
    """Tests the connection with a `listdir(".")` round trip.

    Args:
      logit: Whether to log the failure reason if the connection test fails.

    Returns:
      `True` if the round trip succeeded, `False` otherwise.
    """
    try:
      with self as sftp:
        assert isinstance(sftp.handler, SFTPClient)
        sftp.handler.listdir(".")
      return True
    except Exception:
      if logit:
        logger.exception("%s: Waiting SFTP server is offline.", self.container_cls)
      return False

  @override
  def makedir(self, remote_path: str) -> None:
    """Creates a directory on the server.

    Args:
      remote_path: Absolute path of the directory to create.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.mkdir(remote_path)
