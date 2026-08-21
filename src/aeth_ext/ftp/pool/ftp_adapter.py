"""`FTPAdapter`: fixed one-connection-per-slot pooling for plain FTP. No transport/channel tiers --
each pooled connection is a single, self-contained `FTP`/`FTP_TLS` object, so pooling is just an idle
queue plus `_PooledAdapterBase`'s shared ceiling bookkeeping.
"""

# Standard library imports
from ftplib import FTP
from queue import Empty, Queue
from typing import TYPE_CHECKING, override

# First party imports
from aeth_ext.ftp.connectors import FTPConnector
from aeth_ext.ftp.pool.base import PooledAdapterBase
from aeth_ext.ftp.session import AdaptedFTP
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence
  from contextvars import ContextVar
  from typing import Any
  from zoneinfo import ZoneInfo

  # First party imports
  from aeth_ext.ftp.credentials import FTPCredentials
  from aeth_ext.rich.progress import Progress


SETTINGS = BaseSettings.get_settings()


__all__ = ["FTPAdapter"]


class FTPAdapter(PooledAdapterBase[AdaptedFTP, FTP]):
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
    self._connector = FTPConnector(credentials)
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
    """Closes every idle connection, leaving checked-out ones untouched.

    Decrements `_current_size` per drained handle -- without this, a reused adapter (e.g. tests
    calling `_shutdown_teardown()` directly) would see an inflated size that never comes back down,
    making `_effective_ceiling()` block new checkouts unnecessarily.
    """
    while True:
      try:
        handle = self._idle.get_nowait()
      except Empty:
        break
      with self._size_lock:
        self._current_size -= 1
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
