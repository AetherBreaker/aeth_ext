"""Protocol-agnostic pool bookkeeping (size/ceiling/keepalive/shutdown) shared by `FTPAdapter` and
`SFTPAdapter`. Everything that actually knows about FTP-vs-SFTP shapes (how to check out an idle handle,
how to open a brand new one, how to release/discard) is abstract here and owned entirely by the
concrete subclass -- no isinstance/issubclass branching anywhere in this file.

Also home to `TransportDialer`: the concrete type `ChannelLedger` (`pool.sftp_channel_pool`) depends on
for its `transports` field, instead of a `TransportProvider` Protocol or `SFTPAdapter` itself. Typing
`ChannelLedger.transports` as concrete `SFTPAdapter` would create an import cycle -- `SFTPAdapter`
(`pool.sftp_adapter`) needs a real import of `pool.sftp_channel_pool` to construct `ChannelLedger`/
`SFTPChannelPool`, so `pool.sftp_channel_pool` importing `SFTPAdapter` back (even under
`TYPE_CHECKING`) trips `reportImportCycles`. `TransportDialer` breaks that: it holds two plain
callables and has no dependency on `SFTPAdapter` or `pool.sftp_channel_pool` at all, so
`pool.sftp_channel_pool` can depend on it for real with zero cycle.
"""

# Standard library imports
from abc import ABC, abstractmethod
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

# First party imports
from aeth_ext.errors.shutdown import ShutdownPhase, register_for_shutdown

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from contextvars import ContextVar
  from zoneinfo import ZoneInfo

  # Third party imports
  from paramiko import Transport

  # First party imports
  from aeth_ext.ftp.session import AdapterBase
  from aeth_ext.rich.progress import Progress


class TransportDialer:
  """Dials new `Transport`s within a shared ceiling and records when one dies. Built via
  `PooledAdapterBase._make_transport_dialer` and handed to `ChannelLedger` so the ledger depends on a
  real, narrow, navigable type instead of `SFTPAdapter` itself. See this module's docstring for why a
  plain callable-holder (rather than a reference to the pool that built it) is what avoids the cycle.
  """

  __slots__ = ("open_transport", "transport_dropped")

  def __init__(self, *, open_transport: Callable[[], Transport | None], transport_dropped: Callable[[], None]) -> None:
    """Stores the two bound operations `ChannelLedger` needs.

    Args:
      open_transport: Dials a new `Transport` within the owning pool's ceiling.
      transport_dropped: Records that a previously-dialed `Transport` has died.
    """
    self.open_transport = open_transport
    self.transport_dropped = transport_dropped


class PooledAdapterBase[SessionT: AdapterBase, HandleT](ABC):
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

    Raises:
      ValueError: `max_connections` is less than 1, or `keepalive_interval` is not `None` and not
        positive.
    """
    if max_connections < 1:
      raise ValueError(f"max_connections must be >= 1, got {max_connections}")
    if keepalive_interval is not None and keepalive_interval <= 0:
      raise ValueError(f"keepalive_interval must be positive or None, got {keepalive_interval}")

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
        # A failure that drops _current_size to 0 means no connection exists at all (e.g. the server
        # is down), not that the server has a real ceiling -- pinning _discovered_max to 0 would leave
        # _effective_ceiling() at 0 forever, and callers that fall back to blocking on an empty idle
        # queue (see FTPAdapter.acquire()) would then hang forever with nothing left to release into
        # it. Only record a ceiling when it reflects an actual limit above zero.
        if self._current_size > 0:
          self._discovered_max = self._current_size
          self._discovered_max_last_probe = monotonic()
        raise
      except Exception:
        # Non-OSError failures (e.g. ftplib.error_perm, paramiko's AuthenticationException) still
        # reserved a slot above and must roll it back too -- unlike OSError, these don't reflect a
        # real server-side connection ceiling, so _discovered_max is deliberately left untouched.
        self._current_size -= 1
        raise
      else:
        if self._discovered_max is not None and self._current_size > self._discovered_max:
          self._discovered_max = self._current_size
        return result

  def _make_transport_dialer(self, dial: Callable[[], Transport]) -> TransportDialer:
    """Builds a `TransportDialer` bound to this pool's own ceiling/size bookkeeping.

    Args:
      dial: Opens one new `Transport` (e.g. `_SFTPConnector.get_transport`).

    Returns:
      The dialer, for `SFTPAdapter` to hand to `ChannelLedger`.
    """

    def _open() -> Transport | None:
      return self._open_new_slot(dial)

    def _drop() -> None:
      with self._size_lock:
        self._current_size -= 1

    return TransportDialer(open_transport=_open, transport_dropped=_drop)

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
