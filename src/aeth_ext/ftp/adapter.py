# pyright: reportImportCycles=false
# Standard library imports
from contextlib import nullcontext
from datetime import datetime
from ftplib import FTP, _SSLSocket, all_errors  # type: ignore
from io import BytesIO
from logging import getLogger
from typing import TYPE_CHECKING, override

# Third party imports
from paramiko import SFTPClient, SFTPError

# First party imports
from aeth_ext.ftp.types import AdapterProtocol, BufferSize, FTPProtocol, ListDirResult, SFTPProtocol, TransferSuccess
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Buffer, Callable, Iterator
  from contextvars import ContextVar
  from types import TracebackType
  from typing import Any, Self
  from zoneinfo import ZoneInfo

  # First party imports
  from aeth_ext.rich.progress import Progress


logger = getLogger(__name__)

SETTINGS = BaseSettings.get_settings()


__all__ = ["AdaptedFTP", "AdaptedSFTP", "FTPAdapter", "SFTPProtocol"]


_CONNECTION_FATAL_TYPES = (TimeoutError, ConnectionError, BrokenPipeError, EOFError)


def _is_connection_fatal(exc: BaseException | None) -> bool:
  if exc is None:
    return False
  if isinstance(exc, _CONNECTION_FATAL_TYPES):
    return True
  # Standard library imports
  from paramiko import SSHException

  return isinstance(exc, SSHException)


class AdaptedFTP(AdapterProtocol):
  __slots__ = ("_pool", "container_cls", "handler", "pbar", "proto_instance", "tzinfo")

  def __init__(
    self, ftp_protocol: FTPProtocol, container_cls: str, pbar: Progress | None = None, tzinfo: ZoneInfo = SETTINGS.tz
  ) -> None:
    self.proto_instance = ftp_protocol
    self.handler = None
    self.container_cls = container_cls
    self.pbar = pbar
    self.tzinfo = tzinfo
    self._pool = None  # set by FTPAdapter.start_session() when pool-backed
    super().__init__()

  def __enter__(self) -> Self:
    if self.handler is None:
      self.handler = self.proto_instance.get_conn_handler()
    return self

  def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
    if self._pool is not None:
      if _is_connection_fatal(exc_val):
        self._pool._discard(self.handler)  # pyright: ignore[reportPrivateUsage]
      else:
        self._pool._release(self.handler)  # pyright: ignore[reportPrivateUsage]
    else:
      self.proto_instance.close_conn_handler()

  @override
  def upload_file(self, remote_path: str, callback: Callable[[BufferSize], bytes], file_size: int, task_msg: str = "") -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      self.handler.voidcmd("TYPE I")  # Set binary mode
      with self.handler.transfercmd(f"STOR {remote_path}") as conn:
        with (
          self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=file_size)
          if self.pbar is not None
          else nullcontext() as transfer_task
        ):
          while buffer := callback(8192):
            conn.sendall(buffer)
            if self.pbar is not None:
              assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
              self.pbar.update(transfer_task, advance=len(buffer))
        if _SSLSocket is not None and isinstance(conn, _SSLSocket):
          conn.unwrap()  # type: ignore
    finally:
      self.handler.voidresp()

  @override
  def download_file(self, remote_path: str, callback: Callable[[Buffer], Any], task_msg: str = "") -> None:
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
          while data := conn.recv(8192):
            callback(data)
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
          while data := source_conn.recv(8192):
            if callback is not None:
              callback(data)
            dest_file.write(data)
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
        while data := source_conn.recv(8192):
          if callback is not None:
            callback(data)
          dest_conn.sendall(data)
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
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.voidcmd("TYPE I")  # Set binary mode
    return self.handler.size(path)

  @override
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.rename(old_remote_path, new_remote_path)

  @override
  def remove(self, remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.delete(remote_path)

  @override
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    for entry in self.handler.mlsd(path):
      name, facts = entry
      if "modify" in facts:
        dt = datetime.strptime(facts["modify"], "%Y%m%d%H%M%S")  # noqa: DTZ007
        new_dt = dt.replace(tzinfo=self.tzinfo)
        yield ListDirResult(filename=name, modified_time=new_dt)

  @override
  def test_connection(self, logit: bool = False) -> bool:
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
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.mkd(remote_path)


class AdaptedSFTP(AdapterProtocol):
  __slots__ = ("_pool", "container_cls", "handler", "pbar", "proto_instance", "tzinfo")

  def __init__(
    self, ftp_protocol: SFTPProtocol, container_cls: str, pbar: Progress | None = None, tzinfo: ZoneInfo | None = SETTINGS.tz
  ) -> None:
    self.proto_instance = ftp_protocol
    self.handler = None
    self.container_cls = container_cls
    self.pbar = pbar
    self.tzinfo = tzinfo
    self._pool = None  # set by FTPAdapter.start_session() when pool-backed
    super().__init__()

  def __enter__(self) -> Self:
    if self.handler is None:
      self.handler = self.proto_instance.get_conn_handler()
    return self

  def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
    if self._pool is not None:
      if _is_connection_fatal(exc_val):
        self._pool._discard(self.handler)  # pyright: ignore[reportPrivateUsage]
      else:
        self._pool._release(self.handler)  # pyright: ignore[reportPrivateUsage]
    else:
      self.proto_instance.close_conn_handler()

  @override
  def upload_file(self, remote_path: str, callback: Callable[[BufferSize], bytes], file_size: int, task_msg: str = "") -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    with (
      self.handler.open(remote_path, mode="wb") as remote_file,
      (
        self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=file_size) if self.pbar is not None else nullcontext()
      ) as transfer_task,
    ):
      while buffer := callback(8192):
        remote_file.write(buffer)
        if self.pbar is not None:
          assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
          self.pbar.update(transfer_task, advance=len(buffer))

  @override
  def download_file(self, remote_path: str, callback: Callable[[bytes], Any], task_msg: str = "") -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    with self.handler.open(remote_path, mode="rb") as remote_file:
      size = remote_file.stat().st_size
      remote_file.prefetch(size)
      with (
        self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=size)
        if self.pbar is not None
        else nullcontext() as transfer_task
      ):
        while data := remote_file.read(8192):
          callback(data)
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
        while data := source_file.read(8192):
          if callback is not None:
            callback(data)
          dest_conn.sendall(data)
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
        while data := source_file.read(8192):
          if callback is not None:
            callback(data)
          dest_file.write(data)
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
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      return self.handler.stat(path).st_size
    except SFTPError:
      logger.exception("%s: Failed to get file size for %s.", self.container_cls, path)
      return None

  @override
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.rename(old_remote_path, new_remote_path)

  @override
  def remove(self, remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.remove(remote_path)

  @override
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    for entry in self.handler.listdir_iter(path):
      if entry.st_mtime is None:
        raise ValueError(f"Entry {entry.filename} does not have a modification time, cannot be used in _sftp_listdir")
      yield ListDirResult(filename=entry.filename, modified_time=datetime.fromtimestamp(entry.st_mtime, tz=self.tzinfo))

  @override
  def test_connection(self, logit: bool = False) -> bool:
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
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.mkdir(remote_path)


class FTPAdapter[HandlerType_T: AdaptedFTP | AdaptedSFTP]:
  __slots__ = (
    "_current_size",
    "_idle",
    "_size_lock",
    "container_cls",
    "container_cvar",
    "ftp_protocol",
    "max_connections",
    "pbar",
    "protocol_handler",
    "tzinfo",
  )

  def __init__(
    self,
    ftp_protocol: type[FTPProtocol | SFTPProtocol],
    container_cls: str | None = None,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    container_cvar: ContextVar[str] | None = None,
    max_connections: int = 16,
  ) -> None:
    self.container_cvar = container_cvar
    self.container_cls = container_cls
    self.ftp_protocol = ftp_protocol
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.max_connections = max_connections

    if issubclass(ftp_protocol, FTPProtocol):
      self.protocol_handler = AdaptedFTP
      self.ftp_protocol = ftp_protocol
    elif issubclass(ftp_protocol, SFTPProtocol):  # pyright: ignore[reportUnnecessaryIsInstance]
      self.protocol_handler = AdaptedSFTP
      self.ftp_protocol = ftp_protocol
    else:
      raise TypeError(f"Unsupported protocol type: {ftp_protocol}")  # pyright: ignore[reportUnreachable]

    # Standard library imports
    from queue import Queue
    from threading import Lock

    self._idle = Queue(maxsize=max_connections)
    self._current_size = 0
    self._size_lock = Lock()

    super().__init__()

  def _resolve_container_cls(self) -> str | None:
    try:
      if self.container_cvar is not None:
        return self.container_cvar.get()
      return self.container_cls
    except LookupError:
      return self.container_cls

  def _open_new(self):
    return self.ftp_protocol().get_conn_handler()

  def start_session(self) -> HandlerType_T:
    container_cls = self._resolve_container_cls()

    handler = None
    try:
      handler = self._idle.get_nowait()
    except Exception:  # noqa: BLE001 -- queue.Empty, narrowed in Task 3's validation pass
      pass

    if handler is None:
      with self._size_lock:
        if self._current_size < self.max_connections:
          self._current_size += 1
          handler = self._open_new()

    if handler is None:
      handler = self._idle.get()

    session = self.protocol_handler(self.ftp_protocol(), container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo)  # type: ignore
    session.handler = handler
    session._pool = self  # pyright: ignore[reportAttributeAccessIssue]
    return session  # pyright: ignore[reportReturnType]

  def _release(self, handler) -> None:
    self._idle.put(handler)

  def _discard(self, handler) -> None:
    with self._size_lock:
      self._current_size -= 1
    try:
      self.ftp_protocol().close_conn_handler.__func__(handler)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- best-effort close of an already-broken connection
      pass

  def test_connection(self, logit: bool = False) -> bool:
    return self.start_session().test_connection(logit)
