# Standard library imports
from abc import ABC, abstractmethod
from contextlib import nullcontext
from datetime import datetime
from ftplib import FTP, FTP_TLS, _SSLSocket, all_errors  # type: ignore
from io import BytesIO
from logging import getLogger
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, Protocol, overload, override

# Third party imports
from paramiko import AutoAddPolicy, RejectPolicy, SFTPClient, SFTPError, SSHClient, SSHException

# First party imports
from aeth_ext.errors.shutdown import ShutdownPhase, register_for_shutdown
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials
from aeth_ext.ftp.sftp_pool import ChannelLedger, SFTPChannelPool
from aeth_ext.ftp.types import (
  BufferSize,
  HandleProvider,
  IntrumentCallable,
  ListDirResult,
  ReadCallback,
  TransferSuccess,
  WriteCallback,
)
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Buffer, Callable, Iterator, Sequence
  from contextvars import ContextVar
  from types import TracebackType
  from typing import Any, Self
  from zoneinfo import ZoneInfo

  # Third party imports
  from paramiko import Transport

  # First party imports
  from aeth_ext.rich.progress import Progress


logger = getLogger(__name__)

SETTINGS = BaseSettings.get_settings()


__all__ = ["AdaptedFTP", "AdaptedSFTP", "FTPAdapter", "SFTPAdapter", "create_ftp_adapter"]


_CONNECTION_FATAL_TYPES = (TimeoutError, ConnectionError, BrokenPipeError, EOFError, SSHException)


# ---------------------------------------------------------------------------
# Connectors: private, credentials-driven connection-opening logic. Not a public extension point --
# HandleProvider (aeth_ext.ftp.types) is. Each FTPAdapter/SFTPAdapter builds exactly one of these from
# its credentials and holds it for its whole lifetime.
# ---------------------------------------------------------------------------


class _FTPConnector:
  __slots__ = ("_credentials",)

  def __init__(self, credentials: FTPCredentials) -> None:
    """Stores connection credentials for later connection attempts.

    Args:
      credentials: The FTP server credentials to connect with.
    """
    self._credentials = credentials

  def get_transport(self) -> None:
    """No-op: plain FTP has no separate transport tier to open."""
    return  # no-op: FTP has no separate transport/channel tiers

  def request_handler(self, *_args: object, **_kwargs: object) -> FTP:
    """Opens and authenticates a new `FTP`/`FTP_TLS` connection.

    Accepts and ignores positional/keyword arguments so its call signature matches
    `_SFTPConnector.request_handler`'s (which takes the transport to open a channel on) --
    FTP has no such transport argument to pass.

    Returns:
      The connected, authenticated handle.
    """
    conn = FTP_TLS() if self._credentials.use_tls else FTP()
    conn.connect(
      self._credentials.host,
      self._credentials.port,
      timeout=self._credentials.connect_timeout,  # pyright: ignore[reportArgumentType] -- ftplib's stub omits `| None` even though the real default is None
    )
    conn.login(self._credentials.username, self._credentials.password.get_secret_value())
    conn.set_pasv(self._credentials.passive_mode)
    return conn

  def close_conn_handler(self, handle: FTP) -> None:
    """Closes a connection, falling back to a raw close if the graceful QUIT fails.

    Args:
      handle: The connection to close.
    """
    try:
      handle.quit()
    except OSError:
      handle.close()


class _SFTPConnector:
  __slots__ = ("_credentials",)

  def __init__(self, credentials: SFTPCredentials) -> None:
    """Stores connection credentials for later connection attempts.

    Args:
      credentials: The SFTP server credentials to connect with.
    """
    self._credentials = credentials

  def get_transport(self) -> Transport:
    """Opens and authenticates a new SSH `Transport`, applying the configured host-key policy.

    Returns:
      The connected, authenticated `Transport`.
    """
    client = SSHClient()
    if self._credentials.known_hosts_path is not None:
      client.load_host_keys(str(self._credentials.known_hosts_path))
    else:
      client.load_system_host_keys()
    client.set_missing_host_key_policy(AutoAddPolicy() if self._credentials.host_key_policy == "auto_add" else RejectPolicy())
    client.connect(
      self._credentials.host,
      port=self._credentials.port,
      username=self._credentials.username,
      password=self._credentials.password.get_secret_value() if self._credentials.password is not None else None,
      key_filename=str(self._credentials.private_key_path) if self._credentials.private_key_path is not None else None,
      passphrase=(
        self._credentials.private_key_passphrase.get_secret_value() if self._credentials.private_key_passphrase is not None else None
      ),
      timeout=self._credentials.connect_timeout,
    )
    transport = client.get_transport()
    assert transport is not None
    return transport

  def request_handler(self, transport: Transport) -> SFTPClient:
    """Opens a new SFTP channel.

    Args:
      transport: The `Transport` to open a channel on.

    Returns:
      The new channel.
    """
    return SFTPClient.from_transport(transport)  # pyright: ignore[reportReturnType]

  def close_conn_handler(self, handle: Transport) -> None:
    """Closes the whole `Transport`, and every channel opened on it.

    Args:
      handle: The `Transport` to close.
    """
    handle.close()


# ---------------------------------------------------------------------------
# Session lifecycle: shared between AdaptedFTP/AdaptedSFTP. Holds only the acquire/release/handler
# plumbing -- every transfer-protocol method (upload_file, download_file, transfer_file, ...) is defined
# directly on AdaptedFTP/AdaptedSFTP themselves, never here, so goto-definition on a transfer call always
# lands on the real implementation.
# ---------------------------------------------------------------------------


class _AdaptedSessionBase[HandleT]:
  __slots__ = ("_callbacks", "_provider", "chunk_size", "container_cls", "handler", "pbar", "tzinfo")

  def __init__(
    self,
    provider: HandleProvider[HandleT],
    *,
    container_cls: str | None,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    chunk_size: int = 8192,
    callbacks: Sequence[IntrumentCallable] = (),
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
    self._provider = provider
    self._callbacks = tuple(callbacks)
    self.container_cls = container_cls
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.chunk_size = chunk_size
    super().__init__()

  def __enter__(self) -> Self:
    """Acquires a handle from `provider` on first entry; a no-op on nested/repeat entry.

    Returns:
      This session.
    """
    if self.handler is None:
      self.handler, callbacks = self._provider.acquire()
      self._callbacks = (*self._callbacks, *callbacks)
    return self

  def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
    """Releases the handle, marking it fatal if `exc_val` indicates a broken connection.

    Args:
      exc_type: Unused; part of the context-manager protocol.
      exc_val: The exception raised inside the `with` block, if any.
      exc_tb: Unused; part of the context-manager protocol.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self._provider.release(self.handler, isinstance(exc_val, _CONNECTION_FATAL_TYPES))

  def _notify(self, data: bytes) -> None:
    """Invokes every constructor/acquire-time observer with one transferred chunk."""
    for observer in self._callbacks:
      observer(data)


class AdapterProtocol(Protocol):
  __slots__ = ()

  def test_connection(self, logit: bool = False) -> bool:
    """Tests the connection to the FTP/SFTP server.

    Args:
      logit: Whether to log the failure reason if the connection test fails.

    Returns:
      `True` if the connection succeeded, `False` otherwise.
    """
    raise NotImplementedError

  def get_size(self, path: str) -> int | None:
    """Returns a file's size.

    Args:
      path: Absolute path to a file on the server.

    Returns:
      The file's size in bytes, or `None` if it can't be determined.
    """
    raise NotImplementedError

  def upload_file(self, remote_path: str, callback: ReadCallback, file_size: int, task_msg: str = "") -> None:
    """Uploads a file by pulling its bytes from `callback` until it returns an empty chunk.

    Args:
      remote_path: Absolute destination path on the server.
      callback: Called with the desired chunk size; returns that many bytes, or `b""` when done.
      file_size: Total size of the upload, for progress reporting.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    raise NotImplementedError

  def download_file(self, remote_path: str, callback: WriteCallback, task_msg: str = "") -> None:
    """Downloads a file, pushing each chunk read to `callback`.

    Args:
      remote_path: Absolute source path on the server.
      callback: Called with each chunk read.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    raise NotImplementedError

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

  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the server.

    Args:
      old_remote_path: Absolute current path.
      new_remote_path: Absolute path to rename it to.
    """
    raise NotImplementedError

  def remove(self, remote_path: str) -> None:
    """Deletes a file on the server.

    Args:
      remote_path: Absolute path to the file to delete.
    """
    raise NotImplementedError

  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Lists entries (bare filenames, not full paths) in a directory.

    An entry whose modification time can't be determined is skipped, not yielded with a `None` time.

    Args:
      path: Absolute path to a directory on the server.
    """
    raise NotImplementedError

  def makedir(self, remote_path: str) -> None:
    """Creates a directory on the server.

    Args:
      remote_path: Absolute path of the directory to create.
    """
    raise NotImplementedError


class AdaptedFTP(_AdaptedSessionBase[FTP], AdapterProtocol):
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
            other._notify(data)  # destination-side SFTP instrumentation
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


class AdaptedSFTP(_AdaptedSessionBase[SFTPClient], AdapterProtocol):
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
          other._notify(data)
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


# ---------------------------------------------------------------------------
# Pool base: protocol-agnostic bookkeeping (size/ceiling/keepalive/shutdown) shared by FTPAdapter and
# SFTPAdapter. Everything that actually knows about FTP-vs-SFTP shapes (how to check out an idle handle,
# how to open a brand new one, how to release/discard) is abstract here and owned entirely by the
# concrete subclass -- no isinstance/issubclass branching anywhere in this file.
# ---------------------------------------------------------------------------


class _PooledAdapterBase[SessionT: AdapterProtocol, HandleT](ABC):
  __slots__ = (
    "__weakref__",  # CPython-managed, never assigned directly
    "_current_size",
    "_discovered_max",
    "_discovered_max_last_probe",
    "_keepalive_interval",
    "_keepalive_stop",
    "_keepalive_thread",
    "_registered_for_shutdown",
    "_size_lock",
    "chunk_size",
    "container_cls",
    "container_cvar",
    "max_connections",
    "pbar",
    "tzinfo",
  )

  _REPROBE_INTERVAL: ClassVar[float] = 300.0

  def __init__(
    self,
    *,
    max_connections: int,
    chunk_size: int,
    pbar: Progress | None,
    tzinfo: ZoneInfo | None,
    container_cls: str | None,
    container_cvar: ContextVar[str] | None,
    keepalive_interval: float | None,
  ) -> None:
    """Initializes protocol-agnostic pool bookkeeping shared by `FTPAdapter`/`SFTPAdapter`.

    Args:
      max_connections: Ceiling on concurrently open connections.
      chunk_size: Bytes read/written per I/O call by sessions built from this pool.
      pbar: Progress reporter for sessions to report against, if any.
      tzinfo: Timezone used to localize server-reported modification times.
      container_cls: Fallback label attached to log messages when `container_cvar` is unset or unbound.
      container_cvar: Preferred source for the container-label, resolved fresh per session.
      keepalive_interval: Seconds between keepalive pings on idle connections; `None` disables it.
    """
    self.max_connections = max_connections
    self.chunk_size = chunk_size
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.container_cls = container_cls
    self.container_cvar = container_cvar
    self._keepalive_interval = keepalive_interval

    self._current_size = 0
    self._size_lock = Lock()
    self._discovered_max: int | None = None
    self._discovered_max_last_probe: float = 0.0
    self._keepalive_thread = None
    self._keepalive_stop = None
    self._registered_for_shutdown = False

    super().__init__()

  def _effective_ceiling(self) -> int:
    """Returns the connection-count ceiling to grow against: `max_connections`, capped to a previously
    discovered server-side limit until the re-probe interval next allows testing past it."""
    if self._discovered_max is None:
      return self.max_connections
    if monotonic() - self._discovered_max_last_probe >= self._REPROBE_INTERVAL:
      return self.max_connections  # allow one probe past the discovered ceiling
    return min(self.max_connections, self._discovered_max)

  def _open_new_slot[T](self, dial: Callable[[], T]) -> T | None:
    """Ceiling-checked size-lock bookkeeping around opening a brand new low-level connection (a new
    `FTP` object, or a new `Transport`) -- the one piece of connection-establishment that's genuinely
    identical between FTP and SFTP. Does NOT decide *whether* a new connection needs to be opened at
    all (SFTPAdapter has a branch this can't represent: opening a new channel on an existing under-cap
    Transport needs no new slot and no ceiling check) -- that decision belongs entirely to each
    subclass's own `acquire()`.

    `dial` is called while `_size_lock` is held, matching this code's pre-existing locking behavior
    (this is not a new constraint introduced by this refactor) -- growth is serialized pool-wide, not
    just the counter increment.

    Args:
      dial: Opens the new low-level connection.

    Returns:
      `dial`'s result, or `None` if the ceiling was already reached.
    """
    with self._size_lock:
      if self._current_size >= self._effective_ceiling():
        return None
      self._current_size += 1
      self._ensure_registered_for_shutdown()
      try:
        result = dial()
      except OSError:
        self._current_size -= 1
        self._discovered_max = self._current_size
        self._discovered_max_last_probe = monotonic()
        raise
      else:
        if self._discovered_max is not None and self._current_size > self._discovered_max:
          self._discovered_max = self._current_size
        return result

  def start_session(self) -> SessionT:
    """Builds a new session, resolving `container_cls` from `container_cvar` when set and bound.

    Returns:
      The new session.
    """
    try:
      if self.container_cvar is not None:
        container_cls = self.container_cvar.get()
      else:
        container_cls = self.container_cls
    except LookupError:
      container_cls = self.container_cls
    return self._build_session(container_cls)

  def test_connection(self, logit: bool = False) -> bool:
    """Delegates to a fresh session's `test_connection`.

    Args:
      logit: Whether to log the failure reason if the connection test fails.

    Returns:
      `True` if the connection test succeeded, `False` otherwise.
    """
    return self.start_session().test_connection(logit)

  def _keepalive_loop(self) -> None:
    """Runs `_keepalive_check_one` every `_keepalive_interval` seconds until `_keepalive_stop` is set."""
    while not self._keepalive_stop.wait(timeout=self._keepalive_interval):  # pyright: ignore[reportOptionalMemberAccess]
      self._keepalive_check_one()

  def _ensure_keepalive_started(self) -> None:
    """Starts the keepalive thread on first call if `_keepalive_interval` is configured; a no-op after."""
    if self._keepalive_interval is None or self._keepalive_thread is not None:
      return
    self._keepalive_stop = Event()
    self._keepalive_thread = Thread(target=self._keepalive_loop, name="aeth-ext-ftp-keepalive", daemon=True)
    self._keepalive_thread.start()

  def _shutdown_teardown(self) -> None:
    """Stops the keepalive thread (if running) and closes all idle connections.

    Registered as this adapter's process-shutdown callback; never called directly.
    """
    if self._keepalive_stop is not None:
      self._keepalive_stop.set()
      if self._keepalive_thread is not None:
        self._keepalive_thread.join(timeout=2.0)
    self._teardown_idle()

  def _ensure_registered_for_shutdown(self) -> None:
    """Registers `_shutdown_teardown` for process shutdown on first call; a no-op after."""
    if self._registered_for_shutdown:
      return
    self._registered_for_shutdown = True
    register_for_shutdown(self._shutdown_teardown, phase=ShutdownPhase.THREADED)

  # --- abstract, subclass-owned: no runtime type checks here or in any subclass implementation, since
  # each subclass statically knows its own HandleT/SessionT. FTPAdapter is its own HandleProvider and
  # keeps concrete acquire/release/_validate methods to satisfy that structurally; SFTPAdapter is not --
  # SFTPChannelPool is AdaptedSFTP's provider instead, so SFTPAdapter carries none of the three. ---

  @abstractmethod
  def _build_session(self, container_cls: str | None) -> SessionT:
    """Constructs a new session bound to this adapter as its handle provider.

    Args:
      container_cls: Label to attach to log messages the session emits.

    Returns:
      The new session.
    """
    ...

  @abstractmethod
  def _keepalive_check_one(self) -> None:
    """Pops one idle handle and validates it, discarding it if the validation fails."""
    ...

  @abstractmethod
  def _teardown_idle(self) -> None:
    """Closes every idle connection, leaving checked-out ones untouched."""
    ...


class FTPAdapter(_PooledAdapterBase[AdaptedFTP, FTP]):
  __slots__ = ("_connector", "_idle")

  def __init__(
    self,
    credentials: FTPCredentials,
    *,
    max_connections: int = 16,
    chunk_size: int = 8192,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    container_cls: str | None = None,
    container_cvar: ContextVar[str] | None = None,
    keepalive_interval: float | None = None,
  ) -> None:
    """Builds an FTP connection pool with an initially-empty idle queue.

    Args:
      credentials: The FTP server credentials to connect with.
      max_connections: Ceiling on concurrently open connections.
      chunk_size: Bytes read/written per I/O call by sessions built from this pool.
      pbar: Progress reporter for sessions to report against, if any.
      tzinfo: Timezone used to localize server-reported modification times.
      container_cls: Fallback label attached to log messages when `container_cvar` is unset or unbound.
      container_cvar: Preferred source for the container-label, resolved fresh per session.
      keepalive_interval: Seconds between keepalive pings on idle connections; `None` disables it.
    """
    super().__init__(
      max_connections=max_connections,
      chunk_size=chunk_size,
      pbar=pbar,
      tzinfo=tzinfo,
      container_cls=container_cls,
      container_cvar=container_cvar,
      keepalive_interval=keepalive_interval,
    )
    self._connector = _FTPConnector(credentials)
    self._idle: Queue[FTP] = Queue(maxsize=max_connections)

  def acquire(self) -> tuple[FTP, Sequence[Callable[[bytes], Any]]]:
    """Checks out an idle connection if one validates, else opens (or waits for) a new one.

    Returns:
      The handle, with no handle-scoped observer callbacks (FTP has none to attach).
    """
    try:
      candidate = self._idle.get_nowait()
    except Empty:
      candidate = None
    if candidate is not None and not self._validate(candidate):
      self.release(candidate, is_fatal=True)
      candidate = None

    handle = candidate
    if handle is None:
      handle = self._open_new_slot(lambda: self._connector.request_handler(self._connector.get_transport()))
    if handle is None:
      handle = self._idle.get()

    self._ensure_keepalive_started()
    return handle, ()

  def release(self, handle: FTP, is_fatal: bool) -> None:
    """Discards a handle if fatal, else returns it to the idle queue for reuse.

    Args:
      handle: The handle to return or discard.
      is_fatal: Whether the connection is broken and should be discarded rather than pooled.
    """
    if is_fatal:
      with self._size_lock:
        self._current_size -= 1
      try:
        self._connector.close_conn_handler(handle)
      except Exception:  # noqa: BLE001, S110 -- best-effort close of an already-broken connection
        pass
    else:
      self._idle.put(handle)

  def _validate(self, handle: FTP) -> bool:
    """Checks whether a handle responds to a `NOOP` round trip.

    Args:
      handle: The handle to check.

    Returns:
      `True` if `handle` is still usable.
    """
    try:
      handle.voidcmd("NOOP")
      return True
    except Exception:  # noqa: BLE001 -- any failure means the connection is unusable
      return False

  @override
  def _keepalive_check_one(self) -> None:
    """Pops and validates one idle connection, discarding it if the check fails."""
    try:
      handle = self._idle.get_nowait()
    except Empty:
      return
    self.release(handle, is_fatal=not self._validate(handle))

  @override
  def _teardown_idle(self) -> None:
    """Closes every idle connection, leaving checked-out ones untouched."""
    while True:
      try:
        handle = self._idle.get_nowait()
      except Empty:
        break
      try:
        self._connector.close_conn_handler(handle)
      except Exception:  # noqa: BLE001, S110 -- best-effort close during teardown
        pass

  @override
  def _build_session(self, container_cls: str | None) -> AdaptedFTP:
    """Builds a new `AdaptedFTP` session bound to this adapter as its handle provider.

    Args:
      container_cls: Label to attach to log messages the session emits.

    Returns:
      The new session.
    """
    return AdaptedFTP(self, container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo, chunk_size=self.chunk_size)


class SFTPAdapter(_PooledAdapterBase[AdaptedSFTP, SFTPClient]):
  __slots__ = ("_connector", "_ledger")

  def __init__(
    self,
    credentials: SFTPCredentials,
    *,
    max_connections: int = 16,
    chunk_size: int = 8192,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    container_cls: str | None = None,
    container_cvar: ContextVar[str] | None = None,
    keepalive_interval: float | None = None,
    channels_per_transport: int = 4,
  ) -> None:
    """Builds an SFTP `Transport`/channel pool wired to an initially-empty `ChannelLedger`.

    Args:
      credentials: The SFTP server credentials to connect with.
      max_connections: Ceiling on concurrently open `Transport`s.
      chunk_size: Bytes read/written per I/O call by sessions built from this pool.
      pbar: Progress reporter for sessions to report against, if any.
      tzinfo: Timezone used to localize server-reported modification times.
      container_cls: Fallback label attached to log messages when `container_cvar` is unset or unbound.
      container_cvar: Preferred source for the container-label, resolved fresh per session.
      keepalive_interval: Seconds between keepalive pings on idle channels; `None` disables it.
      channels_per_transport: Maximum channels to multiplex onto a single `Transport`.
    """
    super().__init__(
      max_connections=max_connections,
      chunk_size=chunk_size,
      pbar=pbar,
      tzinfo=tzinfo,
      container_cls=container_cls,
      container_cvar=container_cvar,
      keepalive_interval=keepalive_interval,
    )
    self._connector = _SFTPConnector(credentials)
    self._ledger = ChannelLedger(transports=self)
    pool = SFTPChannelPool(self._ledger, self._connector, channels_per_transport)
    self._ledger.pool = pool

  def open_transport(self) -> Transport | None:
    """Dials a new `Transport` within `max_connections`, for `SFTPChannelPool` to grow into.

    Returns:
      The new `Transport`, or `None` if the ceiling was already reached.
    """
    return self._open_new_slot(self._connector.get_transport)

  def transport_dropped(self) -> None:
    """Records that `SFTPChannelPool` dropped a dead `Transport`, freeing one ceiling slot."""
    with self._size_lock:
      self._current_size -= 1

  @override
  def _keepalive_check_one(self) -> None:
    """Pops one idle channel and validates it, discarding it (and possibly its `Transport`) if the
    validation fails."""
    assert self._ledger.pool is not None
    self._ledger.pool.keepalive_check_one()

  @override
  def _teardown_idle(self) -> None:
    """Closes every tracked `Transport` (and every channel opened on it)."""
    assert self._ledger.pool is not None
    self._ledger.pool.teardown()

  @override
  def _build_session(self, container_cls: str | None) -> AdaptedSFTP:
    """Builds a new `AdaptedSFTP` session bound to `SFTPChannelPool` (not this adapter) as its
    handle provider.

    Args:
      container_cls: Label to attach to log messages the session emits.

    Returns:
      The new session.
    """
    assert self._ledger.pool is not None
    return AdaptedSFTP(self._ledger.pool, container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo, chunk_size=self.chunk_size)


@overload
def create_ftp_adapter(
  credentials: FTPCredentials,
  *,
  max_connections: int = 16,
  chunk_size: int = 8192,
  pbar: Progress | None = None,
  tzinfo: ZoneInfo | None = SETTINGS.tz,
  container_cls: str | None = None,
  container_cvar: ContextVar[str] | None = None,
  keepalive_interval: float | None = None,
) -> FTPAdapter: ...
@overload
def create_ftp_adapter(
  credentials: SFTPCredentials,
  *,
  max_connections: int = 16,
  chunk_size: int = 8192,
  pbar: Progress | None = None,
  tzinfo: ZoneInfo | None = SETTINGS.tz,
  container_cls: str | None = None,
  container_cvar: ContextVar[str] | None = None,
  keepalive_interval: float | None = None,
  channels_per_transport: int = 4,
) -> SFTPAdapter: ...
def create_ftp_adapter(credentials: FTPCredentials | SFTPCredentials, **kwargs: Any) -> FTPAdapter | SFTPAdapter:
  """Builds an `FTPAdapter` or `SFTPAdapter`, chosen by `credentials`'s type.

  Args:
    credentials: FTP or SFTP server credentials; determines which adapter type is built.
    **kwargs: Forwarded to `FTPAdapter`/`SFTPAdapter`'s constructor.

  Returns:
    An `FTPAdapter` for `FTPCredentials`, or an `SFTPAdapter` for `SFTPCredentials`.
  """
  if isinstance(credentials, FTPCredentials):
    return FTPAdapter(credentials, **kwargs)
  return SFTPAdapter(credentials, **kwargs)
