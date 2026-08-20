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
from threading import Condition, Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

# First party imports
from aeth_ext.errors.shutdown import ShutdownPhase, register_for_shutdown
from aeth_ext.ftp.errors import PoolClosedError

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


class WakeupGate:
  """The retry signal behind a blocking `acquire()` fallback, owned by `PooledAdapterBase` and shared
  with `SFTPChannelPool`. Blocking on "wait for an idle handle" alone strands a waiter when capacity
  frees up without ever producing one -- a fatal release just shrinks the size ceiling; a discarded
  channel just shrinks a transport's count. Every path that frees capacity, in whatever form, must
  call `signal()`; `retry_until()` then re-runs the caller's whole acquire decision on each wakeup
  instead of blocking on the one specific source a plain queue read would wake on.

  A bare `Condition` would lose wakeups: a `signal()` landing after `attempt()` returned `None` but
  before the caller parks would notify nobody, and that caller would sleep forever. Closing that
  window by holding the lock across `attempt()` is not an option here -- `attempt()` dials transports
  and opens channels, so it would serialize every acquire behind one another's network I/O. The
  epoch counter resolves it by *detecting* the race rather than excluding it: `retry_until` samples
  the epoch before attempting and only sleeps if it hasn't moved since, so a signal that arrives
  mid-attempt is never missed, only turned into an immediate retry. Nothing accumulates when no one
  is waiting, which a token-per-signal queue could not avoid.
  """

  __slots__ = ("_closed", "_cond", "_epoch")

  def __init__(self) -> None:
    self._cond = Condition()
    self._epoch = 0
    self._closed = False

  def raise_if_closed(self) -> None:
    """Rejects use of a pool whose teardown has run.

    Callers check this at the top of `acquire()` as well: `retry_until` is only reached when the
    first checkout attempt comes up empty, so a gate-internal check alone would let an acquire that
    succeeds outright sail through a torn-down pool.

    Raises:
      PoolClosedError: `close` has been called.
    """
    with self._cond:
      if self._closed:
        raise PoolClosedError("this connection pool has been torn down and can no longer be used")

  def is_closed(self) -> bool:
    """Reports whether `close` has been called, without raising.

    For a `release()` path, where a torn-down pool isn't an error (checked-out handles stay
    releasable) but does mean a non-fatal release shouldn't re-idle its handle -- nothing will ever
    check the idle queue/list again once teardown has run, so it would sit unclosed forever.
    """
    with self._cond:
      return self._closed

  def wait_for_close(self, timeout: float) -> bool:
    """Blocks up to `timeout` seconds, waking early if `close` is called during the wait.

    For a background timer/sweep that would otherwise use a plain `sleep`, so it can retire as soon
    as the pool tears down instead of sleeping out its full interval on a pool nothing can use
    anymore.

    Args:
      timeout: Max seconds to wait.

    Returns:
      Whether the gate is closed -- either already closed when called, or closed during the wait.
      `False` means the wait simply timed out.
    """
    with self._cond:
      if not self._closed:
        self._cond.wait(timeout=timeout)
      return self._closed

  def signal(self) -> None:
    """Wakes one blocked `retry_until` caller, or records that capacity changed if none is parked."""
    with self._cond:
      self._epoch += 1
      self._cond.notify()

  def close(self) -> None:
    """Permanently rejects further acquires and releases every blocked `retry_until` caller.

    Terminal by design -- teardown runs at process shutdown, so there is no reopen. Notifies *all*
    waiters rather than one: every one of them has to leave, and no capacity is being handed out.
    """
    with self._cond:
      self._closed = True
      self._cond.notify_all()

  def retry_until[T](self, attempt: Callable[[], T | None]) -> T:
    """Calls `attempt()` until it returns non-`None`, blocking on `signal()` between failures.

    Args:
      attempt: Re-runs the caller's whole acquire decision; returns `None` if nothing was available.

    Returns:
      `attempt()`'s first non-`None` result.

    Raises:
      PoolClosedError: `close` has been called, either before this call or while it was blocked.
    """
    while True:
      with self._cond:
        self.raise_if_closed()
        seen = self._epoch
      result = attempt()
      if result is not None:
        return result
      with self._cond:
        # Re-checked inside the same critical section as the sleep: a close() or signal() landing
        # during attempt() above must not be slept through. Condition's default lock is an RLock,
        # so raise_if_closed() re-entering here is safe and keeps both checks atomic with the wait.
        self.raise_if_closed()
        if self._epoch == seen:
          self._cond.wait()


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
    "_wakeup",
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
      ValueError: `max_connections` is less than 1, `keepalive_interval` is not `None` and not
        positive, or `chunk_size` is less than 1.
    """
    if max_connections < 1:
      raise ValueError(f"max_connections must be >= 1, got {max_connections}")
    if keepalive_interval is not None and keepalive_interval <= 0:
      raise ValueError(f"keepalive_interval must be positive or None, got {keepalive_interval}")
    if chunk_size < 1:
      raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")

    self.max_connections = max_connections
    self.chunk_size = chunk_size
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.container_cls = container_cls
    self.container_cvar = container_cvar
    self._keepalive_interval = keepalive_interval

    self._current_size = 0
    self._size_lock = Lock()
    self._wakeup = WakeupGate()
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
    try:
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
          # _effective_ceiling() at 0 forever, and callers that fall back to blocking (see
          # FTPAdapter.acquire()) would then hang forever with nothing left to free capacity for them.
          # Only record a ceiling when it reflects an actual limit above zero.
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
    except Exception:
      # Either rollback above returned a slot, so a blocked waiter may now be able to grow. Signalling
      # out here rather than beside each rollback keeps it clear of _size_lock: signal() takes the
      # gate's own lock, and nesting the two would fix a _size_lock -> gate ordering that every future
      # caller would have to honour. The ceiling-reached path returns instead of raising, so it
      # correctly never signals -- nothing was freed.
      self._wakeup.signal()
      raise

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
    with self._size_lock:
      if self._keepalive_thread is not None:
        return
      self._keepalive_stop = Event()
      self._keepalive_thread = Thread(target=self._keepalive_loop, name="aeth-ext-ftp-keepalive", daemon=True)
      self._keepalive_thread.start()

  def _shutdown_teardown(self) -> None:
    """Closes the pool permanently: rejects further acquires, stops the keepalive thread, and closes
    all idle connections.

    Registered as this adapter's process-shutdown callback; never called directly. Terminal -- there
    is no reopen. The gate closes first so blocked waiters fail out immediately rather than racing
    the cleanup below, and so no new acquire can dial a connection into a pool being dismantled.
    Handles already checked out are deliberately left alone and stay releasable, letting sessions
    that started before shutdown run to completion.
    """
    self._wakeup.close()
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
