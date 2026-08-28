"""Per-session transfer logic shared by `AdaptedFTP`/`AdaptedSFTP`. Holds only the acquire/release/handler
plumbing -- every transfer-protocol method (upload_file, download_file, transfer_file, ...) is defined
directly on `AdaptedFTP`/`AdaptedSFTP` themselves, never on a shared mixin, so goto-definition on a
transfer call always lands on the real implementation.
"""

# Standard library imports
from abc import ABC, abstractmethod
from contextlib import contextmanager, nullcontext
from datetime import datetime
from errno import ENOSPC, ENOSYS
from ftplib import FTP, _SSLSocket, all_errors, error_perm, error_proto, error_reply, error_temp  # type: ignore
from io import BytesIO
from logging import getLogger
from typing import TYPE_CHECKING, override

# First party imports
from aeth_ext.ftp.errors import HandleReleasedError
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
  from collections.abc import Callable, Generator, Iterator, Sequence
  from types import TracebackType
  from typing import Self
  from zoneinfo import ZoneInfo

  # Third party imports
  from paramiko import SFTPClient, SFTPError, SSHException

  # First party imports
  from aeth_ext.rich.progress import Progress
  from aeth_ext.types import SizedBuffer

  _PARAMIKO_INSTALLED = True
else:
  try:
    # Third party imports
    from paramiko import SFTPClient, SFTPError, SSHException
  except ImportError:
    # AdaptedSFTP is hidden entirely below when paramiko isn't installed (the optional `sftp`
    # extra) -- plain FTP usage must not require it just to import this module.
    _PARAMIKO_INSTALLED = False
  else:
    _PARAMIKO_INSTALLED = True


logger = getLogger(__name__)

SETTINGS = BaseSettings.get_settings()


__all__ = ["AdaptedFTP", "AdaptedSFTP"]


_CONNECTION_FATAL_TYPES = (
  TimeoutError,
  ConnectionError,
  BrokenPipeError,
  EOFError,
  *((SSHException,) if TYPE_CHECKING or _PARAMIKO_INSTALLED else ()),
)


@contextmanager
def _translate_ftp_errors(path: str, on_550: type[OSError] = FileNotFoundError) -> Generator[None]:
  """Re-raises `ftplib`'s own exceptions as the stdlib `OSError` the reply denotes.

  `ftplib.Error`'s subclasses are not `OSError`s, while paramiko raises `OSError` subclasses for the
  same conditions -- so without this, no single `except` clause in a caller handled both adapters.
  Dispatch is on the three-digit reply code alone; the text after it is server-specific and never
  matched. The narrowest stdlib type that is honest for each code is chosen, and where none exists
  (`452`/`552`/`502`) the `OSError` carries an `errno` a caller can branch on. See `AdapterBase`
  for the resulting per-method contract.

  Args:
    path: The path the command was for, for the resulting message; empty for a reply that is not
      about one path (a transfer's completion reply).
    on_550: What a `550` means for this command. RFC 959 defines it only as "file unavailable",
      which for most commands (`SIZE`, `RETR`, `DELE`, `RNFR`) is a missing file, but for `MKD` is
      usually "already exists" or "parent missing" -- so `makedir` passes plain `OSError`.
  """
  try:
    yield
  except error_proto as e:
    # The reply did not even start with a digit: the control stream is desynchronized and every
    # later reply is untrustworthy, so the handle must not go back to the pool. `ConnectionError`
    # is what `_AdaptedSessionBase.__exit__` classifies as connection-fatal.
    raise ConnectionError(f"{path!r}: malformed FTP reply: {e}" if path else f"malformed FTP reply: {e}") from e
  except error_reply as e:
    # Well-formed but of an unexpected class (a 1xx/3xx where 2xx was required). The stream is
    # still in sync; nothing narrower than `OSError` is honest.
    raise OSError(f"{path!r}: unexpected FTP reply: {e}" if path else f"unexpected FTP reply: {e}") from e
  except (error_perm, error_temp) as e:
    code = str(e)[:3]
    msg = f"{path!r}: {e}" if path else str(e)
    if code in {"421", "426"}:
      # The peer ended the connection: 421 closes the control connection; 426 closed the data
      # connection and aborted the transfer, after which `drain_completion_reply` has already marked
      # the session fatal. A `ConnectionError` subclass so `__exit__` classifies it connection-fatal
      # and the pool drops the handle; `Aborted` rather than the base so it stays distinguishable
      # from a failed dial (`ServerNotAvailableError`) or a socket reset.
      raise ConnectionAbortedError(msg) from e
    if code in {"425", "450"}:
      # 425: no data connection could be opened -- typically the server's passive-port range
      # momentarily exhausted under a parallel wave. 450: file busy. Both succeed on a retry shortly
      # after, and the control connection is healthy, so this must not be a `ConnectionError`
      # (pool-fatal, which would discard a good handle on every port collision). `BlockingIOError`
      # is the stdlib "resource temporarily unavailable" and sits outside `_CONNECTION_FATAL_TYPES`.
      raise BlockingIOError(msg) from e
    if code == "550":
      raise on_550(msg) from e
    if code in {"530", "532", "534", "553"}:
      # Not logged in / need account for storing / denied for policy reasons / name not allowed.
      raise PermissionError(msg) from e
    if code in {"452", "552"}:
      # Insufficient storage / exceeded storage allocation. No stdlib subclass exists for ENOSPC, so
      # the errno is the narrowest signal available.
      raise OSError(ENOSPC, msg) from e
    if code == "502":
      # Command not implemented by this server (e.g. no SIZE/MLSD). Likewise errno-only.
      raise OSError(ENOSYS, msg) from e
    raise OSError(msg) from e


if TYPE_CHECKING or _PARAMIKO_INSTALLED:

  @contextmanager
  def _translate_sftp_errors(path: str) -> Generator[None]:
    """Re-raises paramiko's non-`OSError` failures as `ConnectionError`.

    paramiko already maps every ordinary refusal onto an `OSError` subclass (`FileNotFoundError`,
    `PermissionError`, or a bare errno-less `OSError` for any other status), so those pass through
    untouched. The two paramiko-specific types both mean the channel can no longer be trusted --
    `SFTPError` is a malformed protocol message, `SSHException` a transport failure -- so both become
    `ConnectionError`, which `_AdaptedSessionBase.__exit__` classifies as connection-fatal (as it
    already did for `SSHException` itself).

    Args:
      path: The path the operation was for, for the resulting message.
    """
    try:
      yield
    except (SFTPError, SSHException) as e:
      raise ConnectionError(f"{path!r}: {e}") from e


class _AdaptedSessionBase[HandleT]:
  __slots__ = (
    "_callbacks",
    "_checkout_epoch",
    "_ctor_callbacks",
    "_entries",
    "_fatal",
    "_provider",
    "chunk_size",
    "container_cls",
    "handler",
    "pbar",
    "tzinfo",
  )

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

    Raises:
      ValueError: `chunk_size` is less than 1.
    """
    if chunk_size < 1:
      raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")

    self.handler: HandleT | None = None
    self._entries = 0
    # Bumped on every fresh checkout so lazily-consumed results (see `AdaptedFTP.listdir`) can tell
    # "still the checkout I started under" from "released and checked out again". Handle identity
    # cannot: a pool with few connections routinely hands the very same object back next time.
    self._checkout_epoch = 0
    self._fatal = False
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
      self._checkout_epoch += 1
      # Rebuilt from _ctor_callbacks rather than appended to _callbacks: the provider hands out
      # observers bound to the handle being checked out (for SFTP, to its Transport's throughput
      # state), so appending would keep every previous checkout's observers alive across re-entry --
      # reporting each later chunk to stale state, once more per reuse.
      self._callbacks = (*self._ctor_callbacks, *acquired)
    self._entries += 1
    return self

  def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
    """Releases the handle once the outermost nested `with` exits; marks it fatal if any exit at any
    nesting depth saw an exception indicating a broken connection.

    Args:
      exc_type: Unused; part of the context-manager protocol.
      exc_val: The exception raised inside the `with` block, if any.
      exc_tb: Unused; part of the context-manager protocol.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert self._entries > 0, "__exit__ called without a matching __enter__"
    self._entries -= 1
    # Accumulated rather than read only at depth zero: an inner `with self: ...` whose fatal error is
    # caught before the outer block ends would otherwise be forgotten, and the clean outer exit would
    # release the already-broken handle as non-fatal, straight back into the pool.
    self._fatal = self._fatal or isinstance(exc_val, _CONNECTION_FATAL_TYPES)
    if self._entries > 0:
      # An inner exit of a nested `with self: ...` block -- the outer block still owns the handle.
      return
    self._provider.release(self.handler, self._fatal)
    self.handler = None
    self._fatal = False
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
  def get_size(self, path: str) -> int:
    """Returns a file's size.

    Args:
      path: Absolute path to a file on the server.

    Returns:
      The file's size in bytes.

    Raises:
      FileNotFoundError: No file at `path`.
      PermissionError: The account may not stat it.
      OSError: Any other refusal, or a reply that carried no size.
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

    Raises:
      FileNotFoundError: The destination's parent directory does not exist.
      PermissionError: The account may not write there.
      BlockingIOError: FTP only -- no data connection could be opened (`425`, typically a
        momentarily exhausted passive-port range). Transient; the session stays usable.
      ConnectionError: The connection was lost or the transfer aborted (FTP `421`/`426`); the
        session is discarded from the pool.
      OSError: Any other refusal.
    """
    raise NotImplementedError

  @abstractmethod
  def download_file(self, remote_path: str, callback: WriteCallback, task_msg: str = "") -> None:
    """Downloads a file, pushing each chunk read to `callback`.

    Args:
      remote_path: Absolute source path on the server.
      callback: Called with each chunk read.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.

    Raises:
      FileNotFoundError: No file at `remote_path`.
      PermissionError: The account may not read it.
      BlockingIOError: FTP only -- no data connection could be opened (`425`). Transient; the
        session stays usable.
      ConnectionError: The connection was lost or the transfer aborted (FTP `421`/`426`); the
        session is discarded from the pool.
      OSError: Any other refusal.
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

    Raises:
      FileNotFoundError: No file at `source_remote_path`, or `dest_remote_path`'s parent is missing.
      PermissionError: The account may not read the source or write the destination.
      BlockingIOError: FTP only -- no data connection could be opened (`425`). Transient.
      ConnectionError: Either connection was lost or the transfer aborted (FTP `421`/`426`).
      OSError: Any other refusal.
    """
    raise NotImplementedError

  @abstractmethod
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the server.

    Args:
      old_remote_path: Absolute current path.
      new_remote_path: Absolute path to rename it to.

    Raises:
      FileNotFoundError: No file at `old_remote_path`.
      PermissionError: The account may not rename it.
      OSError: Any other refusal.
    """
    raise NotImplementedError

  @abstractmethod
  def remove(self, remote_path: str) -> None:
    """Deletes a file on the server.

    Args:
      remote_path: Absolute path to the file to delete.

    Raises:
      FileNotFoundError: No file at `remote_path`.
      PermissionError: The account may not delete it.
      OSError: Any other refusal.
    """
    raise NotImplementedError

  @abstractmethod
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Lists entries (bare filenames, not full paths) in a directory.

    An entry whose modification time can't be determined is skipped, not yielded with a `None` time.

    Args:
      path: Absolute path to a directory on the server.

    Raises:
      FileNotFoundError: No directory at `path` -- when the server says so with a `550`; some (e.g.
        pyftpdlib) answer `501` instead, which surfaces as the generic `OSError`.
      PermissionError: The account may not list it.
      OSError: Any other refusal.
    """
    raise NotImplementedError

  @abstractmethod
  def makedir(self, remote_path: str) -> None:
    """Creates a directory on the server.

    Args:
      remote_path: Absolute path of the directory to create.

    Raises:
      PermissionError: The account may not create it.
      OSError: Any other refusal -- including an already-existing directory or a missing parent,
        which neither protocol reports distinguishably (an FTP `550`; an errno-less SFTP status).
    """
    raise NotImplementedError


class AdaptedFTP(_AdaptedSessionBase[FTP], AdapterBase):
  __slots__ = ()

  def drain_completion_reply(self) -> None:
    """Reads this session's own pending FTP completion reply after a data-connection transfer.

    Marks this session fatal before re-raising if the reply itself fails -- e.g. `ftplib.error_temp`
    from a `426` on an aborted transfer. `_CONNECTION_FATAL_TYPES` (checked in `__exit__`) doesn't
    include `ftplib`'s own error classes, and a failure here would otherwise either go unclassified
    (a genuinely desynchronized control connection returned to the pool) or silently replace a
    truly fatal exception from the transfer loop itself (a `finally` block's own exception always
    supersedes whatever was already propagating).

    Not underscore-prefixed despite being outside the public API (absent from `AdaptedFTP`'s own
    surface) -- `_ftp_to_ftp`/`_sftp_to_ftp` call this on `other`, a different session instance of
    the same or a sibling class, and pyright's `reportPrivateUsage` flags any cross-class reference
    to a leading-underscore name, not just cross-module access (see `connectors.py`'s docstring for
    the same reasoning).
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      with _translate_ftp_errors(""):
        self.handler.voidresp()
    except BaseException:
      self._fatal = True
      raise

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
    self.handler.voidcmd("TYPE I")  # Set binary mode
    # transfercmd() itself (not just the loop below) can reject the STOR outright; calling it before
    # the try means a rejection skips voidresp() entirely, matching there being no completion reply
    # pending yet -- draining one here would otherwise block on a reply that will never arrive.
    with _translate_ftp_errors(remote_path):
      conn = self.handler.transfercmd(f"STOR {remote_path}")
    try:
      with conn:
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
      # Reached only once transfercmd() above has actually opened the data connection, so a
      # completion reply is now guaranteed once it closes -- draining it here is always correct,
      # regardless of how the `with` block above exits.
      self.drain_completion_reply()

  @override
  def download_file(self, remote_path: str, callback: WriteCallback, task_msg: str = "") -> None:
    """Streams `remote_path`'s contents from the FTP data connection to `callback`, chunk by chunk.

    Args:
      remote_path: Absolute source path on the server.
      callback: Called with each chunk read.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.voidcmd("TYPE I")  # Set binary mode
    # Resolved before the data connection opens, never after. SIZE travels the *control* connection,
    # and a server that doesn't service that connection mid-transfer (the norm for forking daemons --
    # vsftpd, ProFTPD, Pure-FTPd) leaves this blocked on a reply it won't send until the transfer
    # finishes, while this side isn't draining the data socket for exactly as long. The server then
    # blocks writing into that full socket and neither end moves -- a hard deadlock for any file
    # larger than the socket buffers. Only paid when a progress bar actually needs the total; the
    # 150 reply's own count still wins below whenever the server volunteers one.
    with _translate_ftp_errors(remote_path):
      size = self.handler.size(remote_path) if self.pbar is not None else None
      # ntransfercmd() itself (not just the loop below) can reject the RETR outright; calling it
      # before the try means a rejection skips voidresp() entirely, matching there being no
      # completion reply pending yet -- draining one here would otherwise block on a reply that will
      # never arrive.
      socket, reported_size = self.handler.ntransfercmd(f"RETR {remote_path}")
    if reported_size is not None:
      size = reported_size
    try:
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
      # Reached only once ntransfercmd() above has actually opened the data connection, so a
      # completion reply is now guaranteed once it closes -- draining it here is always correct,
      # regardless of how the `with` block above exits.
      self.drain_completion_reply()

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
    # AdaptedSFTP only exists as a name when paramiko is installed (see this module's top-of-file
    # guard) -- a caller passing something other than AdaptedFTP/AdaptedSFTP while it's missing must
    # still raise TypeError below, not NameError. Real SFTP usage always has paramiko installed by
    # the time this executes (AdaptedSFTP can't be constructed otherwise), so this never changes
    # behavior for a legitimate call.
    elif _PARAMIKO_INSTALLED and isinstance(other, AdaptedSFTP):  # pyright: ignore[reportUnnecessaryIsInstance]
      return self._ftp_to_sftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    else:
      raise TypeError(f"Unsupported other protocol: {other.__class__}")

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
    self.handler.voidcmd("TYPE I")  # Set binary mode
    # Resolved before the data connection opens -- see AdaptedFTP.download_file for why a SIZE issued
    # while a transfer is in flight deadlocks against a server that doesn't read its control
    # connection until the transfer ends. Needed unconditionally here (the size comparison below uses
    # it, not just the progress bar); the 150 reply's own count still wins whenever the server sends one.
    try:
      source_file_size = self.handler.size(source_remote_path)
    except all_errors as e:
      logger.exception("%s: Failed to get source file size for %s", self.container_cls, source_remote_path, exc_info=e)
      source_file_size = None
    # ntransfercmd() itself (not just the loop below) can reject the RETR outright; calling it before
    # the try means a rejection skips voidresp() entirely, matching there being no completion reply
    # pending yet -- draining one here would otherwise block on a reply that will never arrive.
    with _translate_ftp_errors(source_remote_path):
      conn, reported_size = self.handler.ntransfercmd(f"RETR {source_remote_path}")
    if reported_size is not None:
      source_file_size = reported_size
    try:
      with conn as source_conn:
        # Everything below is nested inside the data connection's own context: a failure anywhere in
        # here -- opening the SFTP destination, the progress-bar task -- must still close
        # `source_conn` and drain the completion reply below, exactly like a failure from the
        # transfer loop itself would.
        mem_stream = mem_stream or BytesIO()
        with other.handler.open(dest_remote_path, mode="wb") as dest_file:
          with (
            self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
            if self.pbar is not None
            else nullcontext() as transfer_task
          ):
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
          streamed_file_size = mem_stream.tell()
          try:
            dest_file_size = dest_file.tell()
          except Exception as e:
            dest_file_size = None
            logger.exception("%s: Failed to get destination file size after transfer", self.container_cls, exc_info=e)
            return False
        if _SSLSocket is not None and isinstance(source_conn, _SSLSocket):
          source_conn.unwrap()  # type: ignore
    finally:
      # Reached only once ntransfercmd() above has actually opened the data connection, so a
      # completion reply is now guaranteed once it closes -- draining it here is always correct,
      # regardless of how the `with` block above exits.
      self.drain_completion_reply()
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

  def _ftp_to_ftp(  # noqa: PLR0915, PLR0917
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
    # Resolved before the data connection opens -- see AdaptedFTP.download_file for why a SIZE issued
    # while a transfer is in flight deadlocks against a server that doesn't read its control
    # connection until the transfer ends. Needed unconditionally here (the size comparison below uses
    # it, not just the progress bar); the 150 reply's own count still wins whenever the server sends one.
    try:
      source_file_size = self.handler.size(source_remote_path)
    except all_errors:
      source_file_size = None
      logger.exception("%s: Failed to get source file size.", self.container_cls)
    # ntransfercmd() itself (not just the loop below) can reject the RETR outright; calling it before
    # the try means a rejection skips voidresp() entirely, matching there being no completion reply
    # pending yet -- draining one here would otherwise block on a reply that will never arrive.
    with _translate_ftp_errors(source_remote_path):
      socket, reported_size = self.handler.ntransfercmd(f"RETR {source_remote_path}")
    if reported_size is not None:
      source_file_size = reported_size
    try:
      with socket as source_conn:
        # Everything below is nested inside the source data connection's own context: a failure
        # anywhere in here -- the progress-bar task, destination setup -- must still close
        # `source_conn` and drain the source completion reply below, exactly like a failure from the
        # transfer loop itself would.
        mem_stream = mem_stream or BytesIO()
        with (
          self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
          if self.pbar is not None
          else nullcontext() as transfer_task
        ):
          other.handler.voidcmd("TYPE I")  # Set binary mode
          # transfercmd() itself (not just the loop below) can reject the STOR outright; calling it
          # before the inner try means a rejection skips other.handler.voidresp() entirely, matching
          # there being no completion reply pending yet on that side.
          with _translate_ftp_errors(dest_remote_path):
            dest_conn_cm = other.handler.transfercmd(f"STOR {dest_remote_path}")
          try:
            with dest_conn_cm as dest_conn:
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
              if _SSLSocket is not None and isinstance(dest_conn, _SSLSocket):
                dest_conn.unwrap()  # type: ignore
          finally:
            # Reached only once transfercmd() above has actually opened the destination data
            # connection, so its completion reply is now guaranteed once it closes.
            other.drain_completion_reply()
        if _SSLSocket is not None and isinstance(source_conn, _SSLSocket):
          source_conn.unwrap()  # type: ignore
    finally:
      # Reached only once ntransfercmd() above has actually opened the source data connection, so
      # its completion reply is now guaranteed once it closes -- draining it here is always correct,
      # regardless of how the rest of this method exits.
      self.drain_completion_reply()
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
  def get_size(self, path: str) -> int:
    """Returns a file's size via `SIZE`.

    Args:
      path: Absolute path to a file on the server.

    Returns:
      The file's size in bytes.

    Raises:
      FileNotFoundError: The server replied `550`.
      PermissionError: The server replied `530`/`532`/`534`/`553`.
      OSError: Any other refusal (`errno` `ENOSYS` for a `502`, SIZE unsupported), or a reply that
        was not a `213` and so carried no size.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.voidcmd("TYPE I")  # Set binary mode
    with _translate_ftp_errors(path):
      size = self.handler.size(path)
    if size is None:
      # `ftplib.FTP.size` returns None rather than raising when the reply code is not 213.
      raise OSError(f"{path!r}: SIZE reply carried no size")
    return size

  @override
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the server.

    Args:
      old_remote_path: Absolute current path.
      new_remote_path: Absolute path to rename it to.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    with _translate_ftp_errors(old_remote_path):
      self.handler.rename(old_remote_path, new_remote_path)

  @override
  def remove(self, remote_path: str) -> None:
    """Deletes a file on the server.

    Args:
      remote_path: Absolute path to the file to delete.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    with _translate_ftp_errors(remote_path):
      self.handler.delete(remote_path)

  @override
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Lists entries via `MLSD`, skipping any entry with no `modify` fact.

    Streams from the live connection as it is iterated, so it must be consumed inside the session's
    `with` block. Advancing it afterwards raises rather than reading from a handle the pool has
    already reassigned.

    Args:
      path: Absolute path to a directory on the server.

    Raises:
      HandleReleasedError: The session released its handle before this iterator was exhausted.
    """
    # Captured once: this generator's body first runs on the caller's first next(), which may be
    # long after listdir() returned, so the checkout is re-checked every step against the one this
    # listing actually started under.
    handler = self.handler
    if handler is None:
      raise HandleReleasedError("listdir() was iterated outside the session's `with` block")
    checkout = self._checkout_epoch
    # MLSD is issued and parsed here rather than through `ftplib.FTP.mlsd`, which routes via
    # `retrlines` -- and `retrlines` sends `TYPE A` and never restores `TYPE I`. Because connections
    # are pooled, that left the handle in ASCII for whatever checked it out next, so a later
    # transfer streamed through the server's newline translation and silently corrupted binary
    # payloads: a CRLF file lands two bytes shorter per line and the size check reports a mismatch
    # it cannot account for. MLSD's payload is CRLF-delimited UTF-8 by RFC 3659, so reading it over
    # the binary data connection is well defined -- `TYPE` governs the server's newline handling,
    # not the listing format. Buffered before parsing, exactly as `ftplib.FTP.mlsd` does: the data
    # connection has to be drained and its completion reply read before the caller can issue
    # anything else on the control connection, and a caller abandoning this generator partway would
    # otherwise strand both.
    chunks: list[bytes] = []
    with _translate_ftp_errors(path), handler.transfercmd(f"MLSD {path}" if path else "MLSD") as conn:
      while chunk := conn.recv(self.chunk_size):
        chunks.append(chunk)
    with _translate_ftp_errors(path):
      handler.voidresp()
    for line in b"".join(chunks).split(b"\r\n"):
      if not line:
        continue
      if self.handler is None or self._checkout_epoch != checkout:
        # The session gave this handle back mid-iteration, so the pool may already have handed it to
        # another caller -- reading on would interleave two callers' traffic on one connection.
        raise HandleReleasedError("listdir()'s iterator outlived the session that opened it; consume it inside the `with` block")
      facts_text, _, name = line.decode("utf-8").partition(" ")
      facts = {key.lower(): value for key, _, value in (fact.partition("=") for fact in facts_text[:-1].split(";"))}
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
    with _translate_ftp_errors(remote_path, on_550=OSError):
      self.handler.mkd(remote_path)


if TYPE_CHECKING or _PARAMIKO_INSTALLED:

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
        _translate_sftp_errors(remote_path),
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
      with _translate_sftp_errors(remote_path), self.handler.open(remote_path, mode="rb") as remote_file:
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
      except OSError, SFTPError:
        # OSError as well as SFTPError: paramiko raises SFTPError only for a malformed protocol
        # message, and maps every ordinary refusal -- missing file, permission denied -- to an
        # OSError subclass instead. Catching SFTPError alone let the common case escape a handler
        # whose whole purpose is to let a transfer proceed without a known source size, and made
        # this diverge from the FTP-source paths, which tolerate the same failures via all_errors.
        source_file_size = None
        logger.exception("%s: Failed to get source file size for %s.", self.container_cls, source_remote_path)
      mem_stream = mem_stream or BytesIO()
      with (
        self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
        if self.pbar is not None
        else nullcontext() as transfer_task
      ):
        other.handler.voidcmd("TYPE I")  # Set binary mode
        # transfercmd() itself (not just the loop below) can reject the STOR outright; calling it
        # before the try means a rejection skips voidresp() entirely, matching there being no
        # completion reply pending yet -- draining one here would otherwise block on a reply that
        # will never arrive.
        with _translate_ftp_errors(dest_remote_path):
          dest_conn_cm = other.handler.transfercmd(f"STOR {dest_remote_path}")
        try:
          with (
            dest_conn_cm as dest_conn,
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
        finally:
          # Reached only once transfercmd() above has actually opened the data connection, so a
          # completion reply is now guaranteed once it closes -- draining it here is always correct,
          # regardless of how the `with` block above exits.
          other.drain_completion_reply()
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
      except OSError, SFTPError:
        # OSError as well as SFTPError: paramiko raises SFTPError only for a malformed protocol
        # message, and maps every ordinary refusal -- missing file, permission denied -- to an
        # OSError subclass instead. Catching SFTPError alone let the common case escape a handler
        # whose whole purpose is to let a transfer proceed without a known source size, and made
        # this diverge from the FTP-source paths, which tolerate the same failures via all_errors.
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
    def get_size(self, path: str) -> int:
      """Returns a file's size via `stat`.

      Args:
        path: Absolute path to a file on the server.

      Returns:
        The file's size in bytes.

      Raises:
        FileNotFoundError: No file at `path`.
        PermissionError: The account may not stat it.
        ConnectionError: The channel failed or sent a malformed reply.
        OSError: Any other refusal, or an attribute set with no size.
      """
      assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
      with _translate_sftp_errors(path):
        size = self.handler.stat(path).st_size
      if size is None:
        # paramiko leaves st_size unset when the server omits it from the attribute set.
        raise OSError(f"{path!r}: stat reply carried no size")
      return size

    @override
    def rename(self, old_remote_path: str, new_remote_path: str) -> None:
      """Renames a file on the server.

      Args:
        old_remote_path: Absolute current path.
        new_remote_path: Absolute path to rename it to.
      """
      assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
      with _translate_sftp_errors(old_remote_path):
        self.handler.rename(old_remote_path, new_remote_path)

    @override
    def remove(self, remote_path: str) -> None:
      """Deletes a file on the server.

      Args:
        remote_path: Absolute path to the file to delete.
      """
      assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
      with _translate_sftp_errors(remote_path):
        self.handler.remove(remote_path)

    @override
    def listdir(self, path: str) -> Iterator[ListDirResult]:
      """Lists entries in a directory.

      Streams from the live channel as it is iterated, so it must be consumed inside the session's
      `with` block. Advancing it afterwards raises rather than reading from a channel the pool has
      already reassigned.

      Args:
        path: Absolute path to a directory on the server.

      Raises:
        ValueError: An entry has no modification time -- unlike `AdaptedFTP.listdir`, this does not
          silently skip it.
        HandleReleasedError: The session released its handle before this iterator was exhausted.
      """
      # Captured once; see AdaptedFTP.listdir.
      handler = self.handler
      if handler is None:
        raise HandleReleasedError("listdir() was iterated outside the session's `with` block")
      checkout = self._checkout_epoch
      with _translate_sftp_errors(path):
        entries = handler.listdir_iter(path)
      while True:
        # Checked before advancing, not after. Unlike MLSD -- which does all of its I/O on the first
        # step and then yields from a buffer -- listdir_iter() fetches each further batch from the
        # channel inside its own next(), so a check after the fact would already have put a read
        # onto a channel the pool may have reassigned: the precise interleaving this guards against.
        if self.handler is None or self._checkout_epoch != checkout:
          raise HandleReleasedError("listdir()'s iterator outlived the session that opened it; consume it inside the `with` block")
        try:
          entry = next(entries)
        except StopIteration:
          break
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
      with _translate_sftp_errors(remote_path):
        self.handler.mkdir(remote_path)
