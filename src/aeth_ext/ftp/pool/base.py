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
import multiprocessing.connection as mp_connection
from abc import ABC, abstractmethod
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

# First party imports
from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownPhase, get_current_fatal_trails, register_for_shutdown
from aeth_ext.ftp.errors import PoolClosedError, ServerCapacityError

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from contextvars import ContextVar

  # _ConnectionBase is the actual common base of both platforms' Pipe() halves (Connection on
  # POSIX, PipeConnection on Windows) -- multiprocessing.connection exports neither as a shared
  # public type. Same pattern as pool.sftp_channel_pool's _shutdown_wakeup.
  from multiprocessing.connection import _ConnectionBase  # pyright: ignore[reportPrivateUsage]
  from zoneinfo import ZoneInfo

  # Third party imports
  from paramiko import Transport

  # First party imports
  from aeth_ext.errors.exception_trail import ExceptionTrail
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

  Backed by a pipe (the same `multiprocessing.connection.Pipe` primitive `aeth_ext.errors.shutdown`
  uses for `SHUTDOWN_WAKEUP`) rather than a `Condition`, so that a `signal()` freeing more than one
  slot at once -- a dead one-channel SFTP transport replaced by one with several, or a pruning sweep
  reclaiming several transports in one pass -- wakes *every* blocked `retry_until` caller instead of
  just one. `wait()` on a pipe is level-triggered: any number of threads can block on the same read
  end and all of them return the instant it becomes readable. Once readable, the callers race
  `attempt()` against each other; only one thread drains the one pending byte (`_pending`, guarded by
  `_lock`) so a burst of `signal()` calls collapses into a single wakeup instead of piling up unread
  bytes in the pipe.

  That single shared byte is not enough on its own, though: a caller that has already called
  `attempt()` and gotten `None` but hasn't reached `wait()` yet can lose a `signal()` to a sibling that
  drains the byte first, then blocks in `wait()` on an already-empty pipe with nothing left to wake
  it -- the exact caller a `Condition.notify()` would also strand mid-park. `_epoch` closes that gap:
  it is bumped on every `signal()` regardless of coalescing, and each `retry_until` iteration compares
  its own observed epoch (sampled alongside the drain) against the current one *after* `attempt()`
  fails, skipping `wait()` entirely if a signal landed in between.
  """

  __slots__ = ("_closed", "_epoch", "_lock", "_pending", "_read", "_write")

  def __init__(self) -> None:
    read, write = mp_connection.Pipe(duplex=False)
    self._read: _ConnectionBase = read
    self._write: _ConnectionBase = write
    self._lock = Lock()
    self._pending = False
    self._closed = False
    self._epoch = 0

  def raise_if_closed(self) -> None:
    """Rejects use of a pool whose teardown has run.

    Callers check this at the top of `acquire()` as well: `retry_until` is only reached when the
    first checkout attempt comes up empty, so a gate-internal check alone would let an acquire that
    succeeds outright sail through a torn-down pool.

    Raises:
      PoolClosedError: `close` has been called.
    """
    with self._lock:
      if self._closed:
        raise PoolClosedError("this connection pool has been torn down and can no longer be used")

  def is_closed(self) -> bool:
    """Reports whether `close` has been called, without raising.

    For a `release()` path, where a torn-down pool isn't an error (checked-out handles stay
    releasable) but does mean a non-fatal release shouldn't re-idle its handle -- nothing will ever
    check the idle queue/list again once teardown has run, so it would sit unclosed forever.
    """
    with self._lock:
      return self._closed

  def signal(self) -> None:
    """Wakes every blocked `retry_until` caller, or records that capacity changed if none is parked."""
    with self._lock:
      if self._closed:
        return
      self._epoch += 1
      if self._pending:
        return
      self._pending = True
    try:
      self._write.send_bytes(b"\x00")
    except OSError:
      pass

  def close(self) -> None:
    """Permanently rejects further acquires and releases every blocked `retry_until` caller.

    Terminal by design -- teardown runs at process shutdown, so there is no reopen. Writes
    unconditionally (bypassing the `_pending` coalescing `signal()` does): the pipe only needs to
    end up readable, and every subsequent `retry_until` iteration reads `_closed` before it would
    touch the pipe again, so an extra unread byte left behind here is harmless.
    """
    with self._lock:
      if self._closed:
        return
      self._closed = True
    try:
      self._write.send_bytes(b"\x00")
    except OSError:
      pass

  def retry_until[T](self, attempt: Callable[[], T | None], deadline: Callable[[], float | None] | None = None) -> T:
    """Calls `attempt()` until it returns non-`None`, blocking on `signal()` between failures.

    Args:
      attempt: Re-runs the caller's whole acquire decision; returns `None` if nothing was available.
      deadline: Optional callable returning seconds until some time-based state change makes retrying
        worthwhile even without a `signal()` (e.g. a discovered ceiling's re-probe window elapsing),
        or `None` if there's nothing time-gated to wait for right now. Re-evaluated every iteration,
        since both the remaining time and whether there's a deadline at all can change between
        attempts. Without this, a caller already blocked here when a ceiling was discovered has no
        way to learn its re-probe window later elapsed -- nothing about the passage of time alone
        calls `signal()` -- and could wait long past that window for a `signal()` that may never come.

    Returns:
      `attempt()`'s first non-`None` result.

    Raises:
      PoolClosedError: `close` has been called, either before this call or while it was blocked.
    """
    while True:
      self.raise_if_closed()
      # Drained here, right before attempt(), rather than after wait() below: any signal()s that
      # landed before this point are folded into the decision attempt() is about to make, so stale
      # noise from before this call (or before this thread even started) costs exactly one attempt,
      # not one per signal. observed_epoch is sampled in the same critical section so it reflects
      # exactly what this drain (or lack of one) already accounts for.
      with self._lock:
        observed_epoch = self._epoch
        if self._pending:
          self._pending = False
          try:
            self._read.recv_bytes()
          except OSError:
            pass
      result = attempt()
      if result is not None:
        return result
      self.raise_if_closed()
      # A sibling can drain the shared byte between this attempt() returning None and this thread
      # reaching wait() below, leaving the pipe unreadable with nothing left to wake this thread even
      # though a signal() genuinely happened after observed_epoch was sampled. Checking the epoch here
      # catches that straggler case and retries immediately instead of parking on an empty pipe.
      with self._lock:
        if self._epoch != observed_epoch:
          continue
      # A timeout here is indistinguishable from a real wakeup to the loop above: it just costs one
      # extra attempt() next iteration, exactly like a stale signal() would.
      mp_connection.wait([self._read], timeout=deadline() if deadline is not None else None)


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
    "_shutdown_started",
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
    self._shutdown_started = False

    super().__init__()

  def _effective_ceiling(self) -> int:
    """Returns the connection-count ceiling to grow against: `max_connections`, capped to a previously
    discovered server-side limit until the re-probe interval next allows testing past it."""
    if self._discovered_max is None:
      return self.max_connections
    if monotonic() - self._discovered_max_last_probe >= self._REPROBE_INTERVAL:
      # One slot above the discovered limit, not max_connections outright -- otherwise every
      # concurrent caller sees the cap lifted entirely and can all reserve slots up to
      # max_connections before the first probe even finishes, recreating the exact connection
      # storm this ceiling exists to prevent. A successful probe raises _discovered_max (see
      # _open_new_slot) and refreshes the timestamp, so the next call here sees the new ceiling.
      return min(self.max_connections, self._discovered_max + 1)
    return min(self.max_connections, self._discovered_max)

  def _time_until_reprobe(self) -> float | None:
    """Seconds until `_effective_ceiling()` next allows growth past a discovered ceiling, or `None` if
    there's no discovered ceiling gating growth at all.

    Passed as `retry_until`'s `deadline` so a checkout already blocked at a discovered ceiling gets
    one extra `attempt()` right as the re-probe window opens, even if nothing else ever frees
    capacity (e.g. every existing connection stays checked out) to `signal()` it awake.
    """
    if self._discovered_max is None:
      return None
    return max(0.0, self._REPROBE_INTERVAL - (monotonic() - self._discovered_max_last_probe))

  def _open_new_slot[T](self, dial: Callable[[], T]) -> T | None:
    """Ceiling-checked size-lock bookkeeping around opening a brand new low-level connection (a new
    `FTP` object, or a new `Transport`) -- the one piece of connection-establishment that's genuinely
    identical between FTP and SFTP. Does NOT decide *whether* a new connection needs to be opened at
    all (SFTPAdapter has a branch this can't represent: opening a new channel on an existing under-cap
    Transport needs no new slot and no ceiling check) -- that decision belongs entirely to each
    subclass's own `acquire()`.

    `dial` runs outside `_size_lock`, not while holding it: it's unbounded network I/O (FTP/SFTP
    credentials default `connect_timeout` to `None`), and holding the lock across it would let a
    stalled dial block `_shutdown_teardown()` behind it -- teardown takes this same lock (to read the
    keepalive thread/event, and again per handle `_teardown_idle()` drains) and has its own time
    budget to honor, which a hung connection attempt it can't cancel would blow straight through. The
    slot is reserved under the lock before dialing; the outcome -- grow past a discovered ceiling, or
    roll the reservation back -- is reconciled under the lock again afterward.

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
    except ServerCapacityError:
      with self._size_lock:
        self._current_size -= 1
        # A failure that drops _current_size to 0 means no connection exists at all (e.g. the server
        # is down), not that the server has a real ceiling -- pinning _discovered_max to 0 would leave
        # _effective_ceiling() at 0 forever, and callers that fall back to blocking (see
        # FTPAdapter.acquire()) would then hang forever with nothing left to free capacity for them.
        # Only record a ceiling when it reflects an actual limit above zero.
        has_live_connections = self._current_size > 0
        if has_live_connections:
          self._discovered_max = self._current_size
          self._discovered_max_last_probe = monotonic()
      self._wakeup.signal()  # the rollback freed a slot -- a blocked waiter may now be able to grow
      # With other connections still live, this caller can wait for one of them to free up instead of
      # failing outright -- returning None (rather than re-raising) sends it through acquire()'s normal
      # "nothing available yet" path into the blocking retry loop. Only propagate when nothing is left
      # at all: raising then is correct (see the comment above -- there is no capacity left to wait on).
      if has_live_connections:
        return None
      raise
    except BaseException:
      # Every other failure (OSError -- a timeout, reset, DNS failure, or transient outage --
      # ftplib.error_perm, paramiko's AuthenticationException, KeyboardInterrupt, ...) still
      # reserved a slot above and must roll it back too, but is deliberately NOT treated as a
      # discovered ceiling: only a connector-classified ServerCapacityError (above) means the
      # server actually refused for capacity reasons. A bare OSError is common for reasons that
      # have nothing to do with a real connection-count limit, and would otherwise cap the pool at
      # its current size for a full _REPROBE_INTERVAL on a false signal. BaseException, not
      # Exception: a KeyboardInterrupt landing mid-dial would otherwise leave _current_size
      # incremented forever, and at a ceiling of one that permanently blocks every later acquire on
      # a slot nothing will ever release.
      with self._size_lock:
        self._current_size -= 1
      self._wakeup.signal()
      raise
    else:
      with self._size_lock:
        if self._discovered_max is not None and self._current_size > self._discovered_max:
          self._discovered_max = self._current_size
          # Without this, _discovered_max_last_probe stays at its already-expired value, so
          # _effective_ceiling() keeps returning max_connections for every subsequent caller
          # instead of just the one probe past the old ceiling this growth was meant to be --
          # concurrent checkouts could then all grow straight to max_connections at once.
          self._discovered_max_last_probe = monotonic()
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
    """Starts the keepalive thread on first call if `_keepalive_interval` is configured; a no-op after.

    Guarded by `_shutdown_started` under `_size_lock` rather than just the pre-existing
    `_keepalive_thread is not None` check: without it, a shutdown landing between this method's first
    (lock-free) check and its locked one would see `_keepalive_stop is None`, skip stopping anything,
    and finish -- `_shutdown_teardown` is terminal, so a thread started after that point would never be
    told to stop.
    """
    if self._keepalive_interval is None or self._keepalive_thread is not None:
      return
    with self._size_lock:
      if self._keepalive_thread is not None or self._shutdown_started:
        return
      self._keepalive_stop = Event()
      self._keepalive_thread = Thread(target=self._keepalive_loop, name="aeth-ext-ftp-keepalive", daemon=True)
      self._keepalive_thread.start()

  def _shutdown_teardown(self, trails: tuple[ExceptionTrail, ...]) -> None:
    """Closes the pool permanently: rejects further acquires, stops the keepalive thread, and closes
    all idle connections.

    *trails* is unused: pool teardown is unconditional and doesn't change its behavior based on why
    shutdown was triggered.

    Registered as this adapter's process-shutdown callback; never called directly. Terminal -- there
    is no reopen. The gate closes first so blocked waiters fail out immediately rather than racing
    the cleanup below, and so no new acquire can dial a connection into a pool being dismantled.
    Handles already checked out are deliberately left alone and stay releasable, letting sessions
    that started before shutdown run to completion.

    Reads `_keepalive_stop`/`_keepalive_thread` under `_size_lock` and sets `_shutdown_started` in the
    same critical section as `_ensure_keepalive_started`'s checks, so the two races to a consistent
    outcome either way: if this runs first, the flag stops the thread from ever starting; if
    `_ensure_keepalive_started` runs first, this sees the thread it just started and joins it.
    """
    self._wakeup.close()
    with self._size_lock:
      self._shutdown_started = True
      keepalive_stop = self._keepalive_stop
      keepalive_thread = self._keepalive_thread
    if keepalive_stop is not None:
      keepalive_stop.set()
      if keepalive_thread is not None:
        keepalive_thread.join(timeout=2.0)
    self._teardown_idle()

  def _ensure_registered_for_shutdown(self) -> None:
    """Registers `_shutdown_teardown` for process shutdown on first call; a no-op after.

    `register_for_shutdown` only appends to a list the threaded pass snapshots once -- a shutdown
    that starts between this pool's slot reservation and this registration becoming visible would
    otherwise never tear this pool down, since the pass's snapshot is already taken and won't be
    retaken. Checking `SHUTDOWN.is_set()` right after registering closes that window: run either
    before the snapshot (registration is picked up normally) or after (self-teardown here is the
    only chance). `_shutdown_teardown` tolerates a duplicate call from the rare case both fire.
    """
    if self._registered_for_shutdown:
      return
    self._registered_for_shutdown = True
    register_for_shutdown(self._shutdown_teardown, phase=ShutdownPhase.THREADED)
    if SHUTDOWN.is_set():
      self._shutdown_teardown(get_current_fatal_trails())

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
