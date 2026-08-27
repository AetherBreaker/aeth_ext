"""`SFTPAdapter`: dials `Transport`s within a shared ceiling and hands every channel-multiplexing
decision to `SFTPChannelPool` (`pool.sftp_channel_pool`) via a `ChannelLedger`. `SFTPAdapter` itself
never sees a `SFTPClient` handle or makes an acquire/release decision -- `AdaptedSFTP`'s `HandleProvider`
is `SFTPChannelPool`, not this class.
"""

# Standard library imports
from typing import TYPE_CHECKING, override

# Third party imports
from paramiko import SFTPClient

# First party imports
from aeth_ext.ftp.pool.base import PooledAdapterBase
from aeth_ext.ftp.pool.sftp_channel_pool import ChannelLedger, SFTPChannelPool
from aeth_ext.ftp.session import AdaptedSFTP
from aeth_ext.ftp.sftp_connector import SFTPConnector
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from contextvars import ContextVar
  from zoneinfo import ZoneInfo

  # First party imports
  from aeth_ext.ftp.credentials import SFTPCredentials
  from aeth_ext.rich.progress import Progress


SETTINGS = BaseSettings.get_settings()


__all__ = ["SFTPAdapter"]


class SFTPAdapter(PooledAdapterBase[AdaptedSFTP, SFTPClient]):
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
    acquire_timeout: float | None = 30.0,
    channels_per_transport: int = 10,
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
      acquire_timeout: Seconds a blocked `acquire()` waits for capacity before raising
        `PoolTimeoutError`; `None` waits indefinitely.
      channels_per_transport: Upper bound on channels multiplexed onto a single `Transport`. Lowered
        per-`Transport` if the server refuses a channel open below it (see `TransportState.channel_cap`).

    Raises:
      ValueError: `channels_per_transport` is less than 1.
    """
    if channels_per_transport < 1:
      raise ValueError(f"channels_per_transport must be >= 1, got {channels_per_transport}")
    super().__init__(
      max_connections=max_connections,
      chunk_size=chunk_size,
      pbar=pbar,
      tzinfo=tzinfo,
      container_cls=container_cls,
      container_cvar=container_cvar,
      keepalive_interval=keepalive_interval,
      acquire_timeout=acquire_timeout,
    )
    self._connector = SFTPConnector(credentials)
    self._ledger = ChannelLedger(transports=self._make_transport_dialer(self._connector.get_transport))
    pool = SFTPChannelPool(
      self._ledger,
      self._connector,
      channels_per_transport,
      self._wakeup,
      self._ensure_keepalive_started,
      self._time_until_reprobe,
      acquire_timeout,
    )
    self._ledger.pool = pool

  @override
  def _keepalive_check_one(self) -> None:
    """Pops one idle channel and validates it, discarding it (and possibly its `Transport`) if the
    validation fails."""
    assert self._ledger.pool is not None
    self._ledger.pool.keepalive_check_one()

  @override
  def _teardown_idle(self) -> None:
    """Closes every idle channel (and any `Transport` left with none checked out), leaving
    checked-out ones untouched so sessions that started before shutdown can run to completion and
    still release normally."""
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
