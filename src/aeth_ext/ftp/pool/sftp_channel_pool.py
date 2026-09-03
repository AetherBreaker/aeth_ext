"""Two-tier (Transport, channel) bookkeeping for `SFTPAdapter`'s channel multiplexing.

`ChannelLedger` holds the shared, self-locking state; `SFTPChannelPool` makes every decision on top of
it and is `AdaptedSFTP`'s `HandleProvider`. Kept out of `pool/sftp_adapter.py` so the plain-FTP pooling
path (unchanged fixed one-connection-per-slot queue) isn't diluted by SFTP-only concepts, and kept
entirely out of `AdaptedSFTP` -- that class only ever sees a bare `SFTPClient` handler, identical in
shape to `AdaptedFTP`'s. This module is the only place that knows a checked-out `SFTPClient` came from a
specific `Transport`.
"""

# Standard library imports
import errno
import multiprocessing.connection as mp_connection
from dataclasses import dataclass, field
from threading import Lock, RLock, Thread
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, overload

# Third party imports
from paramiko import ChannelException, SSHException

# First party imports
from aeth_ext.errors.shutdown import SHUTDOWN_WAKEUP
from aeth_ext.ftp.errors import PoolClosedError

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence

  # _ConnectionBase is the actual common base of both platforms' Pipe() halves (Connection on
  # POSIX, PipeConnection on Windows) -- multiprocessing.connection exports neither of those as a
  # shared public type.
  from multiprocessing.connection import _ConnectionBase  # pyright: ignore[reportPrivateUsage]
  from typing import Any

  # Third party imports
  from paramiko import SFTPClient, Transport

  # First party imports
  from aeth_ext.ftp.pool.base import TransportDialer, WakeupGate
  from aeth_ext.ftp.sftp_connector import SFTPConnector
  from aeth_ext.ftp.types import InstrumentCallable
  from aeth_ext.types import SizedBuffer

__all__ = ["Channel", "ChannelLedger", "SFTPChannelPool", "TransportState"]


def _mirror_builtin[F: Callable[..., Any]](source: Callable[..., Any]) -> Callable[[F], F]:
  """Best-effort: copies `source`'s docstring onto the decorated method, plus any real annotations
  `source` happens to carry. Builtins like `dict.pop`/`list.remove` never carry runtime annotations,
  so the latter is a no-op in practice -- `_LockedDict`/`_LockedList` methods keep their own hand-written
  generic annotations as the source of truth regardless. Deliberately skips `functools.wraps`, which
  would also set `__wrapped__`: `inspect.signature` follows that and tries to introspect the builtin's
  C signature, raising `ValueError` instead of reporting this method's own signature.
  """

  def decorator(func: F) -> F:
    func.__doc__ = source.__doc__
    if hasattr(source, "__annotations__"):
      func.__annotations__ = {**source.__annotations__, **func.__annotations__}
    return func

  return decorator


@dataclass(slots=True)
class TransportState:
  """Per-`Transport` bookkeeping: how many channels it currently holds and its measured throughput."""

  transport: Transport
  channel_count: int = 0
  channel_cap: int | None = None
  """Channel ceiling this `Transport` has demonstrably refused to exceed, or `None` while
  `SFTPChannelPool.channels_per_transport` is still believed. Set from a server-side channel-open
  refusal (see `SFTPChannelPool._checkout_idle_or_grow`), never raised again -- an SSH server's
  per-connection session limit is fixed for the connection's lifetime."""
  ewma_throughput: float | None = None
  sample_count: int = 0
  last_released: float = field(default_factory=monotonic)
  """`monotonic()` timestamp of `SFTPChannelPool._discard`'s most recent call for this Transport.
  Read by the prune thread only when `channel_count == 0`, where it means "since when has this
  Transport had no channels" -- an eligible-for-pruning Transport's age is always computed fresh
  against this live value, so a Transport that gets reused and re-emptied is judged by its most
  recent empty spell, never a stale one.

  Defaults to construction time, not 0.0. Every path that drops `channel_count` to zero stamps this
  first, so the default is only ever read for a state that has never been emptied -- but 0.0 would
  make that state read as empty since the monotonic epoch, i.e. instantly past
  `_EMPTY_TRANSPORT_TTL`, so any gap in that stamping would hand the pruner a Transport to reap
  rather than a bounded grace period."""

  _EWMA_ALPHA: ClassVar[float] = 0.3
  _MIN_SAMPLES: ClassVar[int] = 3
  _SATURATION_RATIO: ClassVar[float] = 0.6

  def update_throughput(self, nbytes: int, elapsed: float) -> None:
    """Blends a new sample into the running EWMA throughput estimate.

    Args:
      nbytes: Bytes transferred in this sample.
      elapsed: Seconds elapsed for this sample.
    """
    rate = nbytes / max(elapsed, 1e-6)
    if self.ewma_throughput is None:
      self.ewma_throughput = rate
    else:
      self.ewma_throughput = self._EWMA_ALPHA * rate + (1 - self._EWMA_ALPHA) * self.ewma_throughput
    self.sample_count += 1

  def is_saturated(self, best_throughput: float) -> bool:
    """Reports whether this transport's throughput is meaningfully below `best_throughput`.

    Args:
      best_throughput: The best throughput currently observed across all transports.

    Returns:
      `True` if saturated; always `False` until at least `_MIN_SAMPLES` samples have been recorded.
    """
    if self.ewma_throughput is None or self.sample_count < self._MIN_SAMPLES:
      return False
    return self.ewma_throughput < best_throughput * self._SATURATION_RATIO


@dataclass(slots=True)
class Channel:
  """A checked-out or idle SFTP handle, tagged with the `TransportState` it was opened from."""

  handle: SFTPClient
  state: TransportState


class _Missing:
  """Type of `_MISSING`, the "no default was passed" marker for `_LockedDict.pop`. A dedicated class
  rather than a bare `object()` so the two states are distinguishable to a type checker.
  """

  __slots__ = ()


_MISSING = _Missing()


class _LockedDict[K, V]:
  """A dict whose mutating/reading operations are individually atomic under one shared lock.

  Mirrors `dict`'s own contract exactly -- no method here does anything a plain `dict` method
  wouldn't (same exceptions, same return values). `values()` copies under the lock and returns a
  plain list, so callers never iterate while holding the lock; callers who need what `clear()`
  removed should snapshot `values()` first.
  """

  def __init__(self, lock: RLock) -> None:
    self._data: dict[K, V] = {}
    self._lock = lock

  @_mirror_builtin(dict.__setitem__)
  def __setitem__(self, key: K, value: V) -> None:
    with self._lock:
      self._data[key] = value

  @_mirror_builtin(dict.__getitem__)
  def __getitem__(self, key: K) -> V:
    with self._lock:
      return self._data[key]

  @_mirror_builtin(dict.__delitem__)
  def __delitem__(self, key: K) -> None:
    with self._lock:
      del self._data[key]

  @_mirror_builtin(dict.__contains__)
  def __contains__(self, key: K) -> bool:
    with self._lock:
      return key in self._data

  @_mirror_builtin(dict.__len__)
  def __len__(self) -> int:
    with self._lock:
      return len(self._data)

  @_mirror_builtin(dict.get)
  def get(self, key: K, default: V | None = None) -> V | None:
    with self._lock:
      return self._data.get(key, default)

  @overload
  def pop(self, key: K) -> V: ...

  @overload
  def pop(self, key: K, default: V | None) -> V | None: ...

  @_mirror_builtin(dict.pop)
  def pop(self, key: K, default: V | _Missing | None = _MISSING) -> V | None:
    with self._lock:
      # Forwarding a default unconditionally would turn a missing key into a silent None even when
      # the caller passed no default, hiding bookkeeping bugs that dict.pop() would have raised on.
      if isinstance(default, _Missing):
        return self._data.pop(key)
      return self._data.pop(key, default)

  @_mirror_builtin(dict.values)
  def values(self) -> list[V]:
    with self._lock:
      return list(self._data.values())

  @_mirror_builtin(dict.clear)
  def clear(self) -> None:
    with self._lock:
      self._data.clear()


class _LockedList[T]:
  """A list whose mutating/reading operations are individually atomic under one shared lock.

  Mirrors `list`'s own contract exactly -- same exceptions (`pop()` on empty raises `IndexError`,
  `remove()` on a missing item raises `ValueError`), same return values.
  """

  def __init__(self, lock: RLock) -> None:
    self._data: list[T] = []
    self._lock = lock

  @_mirror_builtin(list.append)
  def append(self, item: T) -> None:
    with self._lock:
      self._data.append(item)

  @_mirror_builtin(list.pop)
  def pop(self) -> T:
    with self._lock:
      return self._data.pop()

  @_mirror_builtin(list.remove)
  def remove(self, item: T) -> None:
    with self._lock:
      self._data.remove(item)

  @_mirror_builtin(list.__contains__)
  def __contains__(self, item: T) -> bool:
    with self._lock:
      return item in self._data

  @_mirror_builtin(list.__len__)
  def __len__(self) -> int:
    with self._lock:
      return len(self._data)

  @_mirror_builtin(list.copy)
  def copy(self) -> list[T]:
    with self._lock:
      return self._data.copy()

  @_mirror_builtin(list.clear)
  def clear(self) -> None:
    with self._lock:
      self._data.clear()


class ChannelLedger:
  """Shared, self-locking books for `SFTPChannelPool`. Holds data only -- every read/write here is a
  single, obvious operation; anything that has to touch more than one of these atomically (e.g. "is this
  transport saturated, and if not, put its channel back in `idle`") is `SFTPChannelPool`'s job, done with
  an explicit `with ledger.lock:` block, not a method on this class.
  """

  def __init__(self, transports: TransportDialer) -> None:
    self.lock = RLock()
    self.transports = transports
    self.pool: SFTPChannelPool | None = None  # filled in by SFTPAdapter once the pool exists
    self.states: _LockedDict[int, TransportState] = _LockedDict(self.lock)
    self.handle_states: _LockedDict[int, TransportState] = _LockedDict(self.lock)
    self.idle: _LockedList[Channel] = _LockedList(self.lock)
    self.in_flight = 0
    self.wave_running_max = 0.0
    self.last_wave_best_throughput: float | None = None


class SFTPChannelPool:
  """Owns every acquire/release/growth/saturation decision on top of a `ChannelLedger`. `AdaptedSFTP`'s
  `HandleProvider`.
  """

  _EMPTY_TRANSPORT_TTL: ClassVar[float] = 30.0

  def __init__(  # noqa: PLR0917
    self,
    ledger: ChannelLedger,
    connector: SFTPConnector,
    channels_per_transport: int,
    wakeup: WakeupGate,
    ensure_keepalive_started: Callable[[], None],
    time_until_reprobe: Callable[[], float | None],
    acquire_timeout: float | None = None,
    shutdown_wakeup: _ConnectionBase = SHUTDOWN_WAKEUP,
  ) -> None:
    """Initializes an empty pool bound to `ledger`'s state and `connector`'s connection-opening.

    Args:
      ledger: The shared bookkeeping this pool reads and writes.
      connector: Opens channels on an existing `Transport` and closes both channels and whole `Transport`s.
      channels_per_transport: Upper bound on channels multiplexed onto a single `Transport`. Lowered
        per-`Transport` if the server refuses a channel open below it (see `TransportState.channel_cap`).
      wakeup: The owning `SFTPAdapter`'s gate, shared rather than built here so that adapter-level
        capacity changes (a rolled-back `Transport` dial) and pool-level ones (a discarded channel)
        wake the same waiters, and so one `close()` at teardown covers both tiers.
      ensure_keepalive_started: Starts the owning `SFTPAdapter`'s keepalive thread on first call.
        Called from `acquire()`, not the constructor -- starting it as soon as a session is merely
        *built* (before any handle is ever acquired) would run the keepalive thread, and register a
        shutdown callback, for a session that's never entered and never opens a connection.
      time_until_reprobe: The owning `SFTPAdapter`'s `_time_until_reprobe`, passed to `retry_until` as
        `acquire()`'s blocking-retry deadline so a checkout blocked at a discovered ceiling isn't
        stranded past the re-probe window with no `signal()` ever coming.
      acquire_timeout: Seconds a blocked `acquire()` waits for a channel before raising
        `PoolTimeoutError`; `None` waits indefinitely. Owned by the adapter and passed down rather
        than re-derived here, so both tiers honour one configured budget.
      shutdown_wakeup: What the prune thread watches to retire early instead of sleeping out
        `_EMPTY_TRANSPORT_TTL` on process shutdown. Defaults to the real process-wide
        `SHUTDOWN_WAKEUP`; overridable so a test can substitute a throwaway pipe instead of
        tripping that one-shot global signal for real.
    """
    self._ledger = ledger
    self._time_until_reprobe = time_until_reprobe
    self._acquire_timeout = acquire_timeout
    self._connector = connector
    self.channels_per_transport = channels_per_transport
    self._wakeup = wakeup
    self._ensure_keepalive_started = ensure_keepalive_started
    self._shutdown_wakeup = shutdown_wakeup
    # Outermost lock in this class: taken only on the dial path, and always before ledger.lock,
    # _size_lock (via open_transport) or the gate. Nothing acquires those and then reaches for this.
    self._dial_lock = Lock()
    # Guards _prune_thread's None-vs-running transition; always taken before ledger.lock, never
    # nested the other way around. See _prune_loop's exit check for why that ordering matters.
    self._prune_lock = Lock()
    self._prune_thread: Thread | None = None

  def acquire(self) -> tuple[SFTPClient, Sequence[InstrumentCallable]]:
    """Checks out an idle channel if one validates, else multiplexes a new channel onto an
    under-cap `Transport`, dials a brand new `Transport`, or (if the pool is fully saturated)
    blocks until a channel is released.

    Returns:
      The handle, plus a throughput-instrumentation observer callback for it.

    Raises:
      PoolClosedError: The owning adapter's shutdown teardown has already run.
      PoolTimeoutError: `acquire_timeout` elapsed with every `Transport` still at capacity.
    """
    self._wakeup.raise_if_closed()  # retry_until below only sees closure once a checkout comes up empty
    channel = self._checkout_idle_or_grow()
    if channel is None:
      # A retry loop that only re-checked ledger.idle would strand this caller when capacity frees up
      # without ever producing an idle channel (e.g. a fatal release of a dead-transport's last live
      # sibling). retry_until() re-runs the whole idle-then-growth decision on every wakeup instead.
      channel = self._wakeup.retry_until(self._checkout_idle_or_grow, deadline=self._time_until_reprobe, timeout=self._acquire_timeout)

    self._ensure_keepalive_started()
    return channel.handle, (self._make_instrument(channel.state),)

  def _rollback_channel_reservation(self, target: TransportState) -> None:
    """Undoes a channel-slot reservation on `target`, tearing the Transport down too if that leaves
    it empty. Shared by `_checkout_idle_or_grow`'s two rollback paths: a failed channel-open, and a
    channel-open that succeeded but completed after the pool's shutdown teardown already ran.

    Keying teardown off "did I dial this one" instead fails both ways: a sibling that reserved a
    slot while this open was in flight would get its Transport closed underneath it, and if that
    sibling's own open then failed too, the Transport would stay registered at zero channels --
    permanently failing every growth attempt that picks it, its slot never returned. Decrement,
    zero-check and states.pop share one critical section, so teardown fires exactly once: once the
    state is gone, no later `_pick_growth_target()` can find it to reserve on, so no second rollback
    can reach zero again.
    """
    with self._ledger.lock:
      target.channel_count -= 1
      abandoned = target.channel_count == 0 and self._ledger.states.pop(id(target.transport), None) is not None
    if abandoned:
      self._connector.close_transport_handler(target.transport)
      self._ledger.transports.transport_dropped()

  def _record_channel_refusal(self, target: TransportState) -> bool:
    """Rolls back a server-refused channel reservation and pins `target`'s cap to what is live.

    A refusal means `channels_per_transport` sits above what this `Transport` actually allows (e.g.
    OpenSSH `MaxSessions`). Left unrecorded, `_pick_growth_target()` keeps choosing it -- it still
    looks under-cap -- and every later acquire fails identically instead of dialing a fresh
    `Transport` against the remaining `max_connections`. Each refusal either lowers a cap or (with
    nothing live) drops the `Transport`, so growth cannot loop on this.

    Args:
      target: The `TransportState` whose channel open was refused.

    Returns:
      `True` if a cap was recorded, meaning growth can retry elsewhere. `False` when the refusal
      left no live channel at all: that is no per-`Transport` ceiling to route around (a cap of 0
      would only skip a `Transport` the rollback already tore down), and nothing distinguishes it
      from any other failed channel-open, so the caller should propagate.
    """
    self._rollback_channel_reservation(target)
    with self._ledger.lock:
      live = target.channel_count
      if live > 0:
        target.channel_cap = live
    self._wakeup.signal()  # the rollback freed a channel slot (and maybe a whole Transport)
    return live > 0

  def _dial_new_transport(self) -> TransportState | None:
    """Dials and registers a brand new `Transport` within the pool's ceiling, with its first channel
    slot already reserved.

    Returns:
      The registered `TransportState`, or `None` if the ceiling was already reached.

    Raises:
      PoolClosedError: The owning adapter's shutdown teardown ran before the dial started.
    """
    with self._dial_lock:
      # Re-pick before dialing. This lock is the only thing serializing cold-start callers --
      # open_transport()'s dial itself runs outside the adapter's _size_lock (a stalled dial must
      # not block _shutdown_teardown() behind it) -- but each would still be acting on the
      # "nothing to grow onto" answer its caller got, from before the thread ahead of it registered
      # its Transport, and would dial its own. The pool would settle at one single-channel Transport
      # per caller and never consolidate, since _pick_growth_target() prefers the lowest
      # channel_count and so keeps spreading later growth across them instead of filling them.
      # Callers that found a target before calling here never reach this, so multiplexing onto
      # existing Transports stays fully parallel -- only dialing, already single-file, is gated.
      target = self._reserve_on_existing()
      if target is not None:
        return target
      # A caller that already cleared acquire()'s raise_if_closed() before teardown() ran could
      # otherwise still dial a brand new Transport into a pool that just tore itself down --
      # teardown()'s snapshot of ledger.states is already taken by then, so nothing would ever
      # close this one. Re-checked here, right before the network call, to close that window.
      self._wakeup.raise_if_closed()
      try:
        transport = self._ledger.transports.open_transport()
      except ConnectionError:
        # Mirrors _open_new_slot's ServerCapacityError policy one tier up. With other Transports
        # still live, their channels will free up, so a transient dial failure (refused, timed out,
        # DNS, network blip -- all ServerNotAvailableError, all ConnectionError) should send this
        # caller through acquire()'s normal "nothing available yet" path rather than failing an
        # acquire the pool can still serve. Only propagate when nothing is live at all: there is no
        # capacity left to wait on then, and blocking would just burn acquire_timeout.
        #
        # ConnectionError specifically, not SSHException: a rejected credential or host key is an
        # AuthenticationException/BadHostKeyException, and no amount of waiting fixes either.
        if not any(state.transport.is_active() for state in self._ledger.states.values()):
          raise
        return None
      if transport is None:
        return None
      if self._wakeup.is_closed():
        # The check above only covers up to the dial, not across it: shutdown can close the gate and
        # take teardown()'s ledger.states snapshot while this dial is still in flight. Registering
        # the Transport now would add it to a pool whose one-shot teardown has already run and will
        # never see it. Roll the reservation back instead.
        self._connector.close_transport_handler(transport)
        self._ledger.transports.transport_dropped()
        self._wakeup.signal()  # the rollback freed a whole Transport's worth of slots
        return None
      with self._ledger.lock:
        target = TransportState(transport=transport, channel_count=1)
        self._ledger.states[id(transport)] = target
      return target

  def _checkout_idle_or_grow(self) -> Channel | None:
    """Tries an idle channel (revalidating it), else multiplexes onto an under-cap `Transport` or
    dials a brand new one within the pool's ceiling.

    Returns:
      A usable channel, or `None` if none of the above is currently available.
    """
    channel = self._checkout_idle()
    if channel is not None and not self._validate(channel.handle):
      # Released fatally rather than discarded: _discard() drops the channel alone, so if the
      # validation failed because the whole Transport died, its state would stay registered holding a
      # _current_size slot until some later growth attempt picked it, failed to open on it, and
      # handed that failure to an unlucky caller. release() runs the is_active() check and cascades
      # to the whole Transport when it is gone; for a live one it discards exactly as before. Matches
      # keepalive_check_one(), which already routes its own failed validation this way.
      self.release(channel.handle, is_fatal=True)
      channel = None

    if channel is None:
      target = self._reserve_on_existing() or self._dial_new_transport()
      if target is not None:
        try:
          handle = self._connector.request_handler(target.transport)
        except ChannelException:
          if self._record_channel_refusal(target):
            # Returning None (rather than looping here) sends this caller through acquire()'s normal
            # "nothing available yet" path, which re-runs the whole decision against the cap just
            # recorded and so picks a different Transport or dials a fresh one.
            return None
          raise
        except BaseException:
          # BaseException, not Exception: a KeyboardInterrupt during SFTPClient.from_transport()
          # would otherwise leave this channel slot reserved with no handle to release it. With
          # channels_per_transport=1 and a one-transport ceiling, that permanently blocks every
          # later acquire on a slot nothing will ever free.
          self._rollback_channel_reservation(target)
          self._wakeup.signal()  # the rollback freed a channel slot (and maybe a whole Transport)
          raise
        if self._wakeup.is_closed():
          # Same race one level down: the channel-open call above can itself outlive a shutdown that
          # already snapshotted the ledger for teardown. Roll back exactly like the exception path
          # above rather than handing out a channel nothing will ever close.
          self._rollback_channel_reservation(target)
          self._connector.close_conn_handler(handle)
          self._wakeup.signal()  # the rollback freed a channel slot (and maybe a whole Transport)
          raise PoolClosedError("this connection pool has been torn down and can no longer be used")
        with self._ledger.lock:
          self._ledger.handle_states[id(handle)] = target
          self._ledger.in_flight += 1
        channel = Channel(handle=handle, state=target)

    return channel

  def release(self, handle: SFTPClient, is_fatal: bool) -> None:
    """Returns a handle to its `Transport`'s pool, or discards it (and the whole `Transport` if it's
    no longer active) when `is_fatal` marks it broken.

    Args:
      handle: The handle to return or discard.
      is_fatal: Whether the connection is broken and should be discarded rather than pooled.
    """
    state = self._ledger.handle_states.get(id(handle))
    assert state is not None, "handle must have been tracked at checkout"
    channel = Channel(handle=handle, state=state)

    if not is_fatal:
      if self._release_or_pop_saturated(channel):
        self._connector.close_conn_handler(channel.handle)
      return

    if state.transport.is_active():
      self._discard(channel)
      return

    # The channel being released is checked-out, not idle, so _drop_transport()'s idle-only sweep
    # never sees it -- untrack and return it here, matching what _discard() does for the live-transport
    # case above.
    with self._ledger.lock:
      self._ledger.handle_states.pop(id(handle), None)
    self._mark_returned(state)

    was_first_to_drop, orphaned = self._drop_transport(state)
    for orphan in (channel, *orphaned):
      self._connector.close_conn_handler(orphan.handle)
    if was_first_to_drop:
      # Concurrently-checked-out siblings on the same dying transport each reach this branch and each
      # call _drop_transport() -- only the first should count against _current_size, since it was only
      # ever incremented once at dial time.
      self._ledger.transports.transport_dropped()
    self._wakeup.signal()  # _current_size just dropped -- a blocked waiter may now be able to grow

  def teardown(self) -> None:
    """Closes every idle channel (and any `Transport` left with none checked out), leaving
    checked-out channels and their `Transport`s alone so their sessions can run to completion and
    still release normally.

    Reports each closed `Transport` to `ledger.transports.transport_dropped()` so `_current_size`
    ends up consistent with what is actually open. Nothing reopens a pool after this -- the gate is
    already closed by the time `SFTPAdapter._teardown_idle` calls here -- but leaving the count
    inflated would misreport the pool's state to anything still inspecting it during shutdown.
    """
    with self._ledger.lock:
      idle_channels = self._ledger.idle.copy()
      self._ledger.idle.clear()
      for channel in idle_channels:
        channel.state.channel_count -= 1
        self._ledger.handle_states.pop(id(channel.handle), None)
      emptied = [s for s in self._ledger.states.values() if s.channel_count == 0]
      for state in emptied:
        self._ledger.states.pop(id(state.transport), None)
    for channel in idle_channels:
      self._connector.close_conn_handler(channel.handle)
    for state in emptied:
      self._connector.close_transport_handler(state.transport)
      self._ledger.transports.transport_dropped()

  def keepalive_check_one(self) -> None:
    """Pops and validates one idle channel, discarding it (and possibly its `Transport`) if the
    check fails.
    """
    channel = self._checkout_idle()
    if channel is None:
      return
    self.release(channel.handle, is_fatal=not self._validate(channel.handle))

  def _checkout_idle(self) -> Channel | None:
    """Pops an idle channel for reuse, or returns `None` if none is idle."""
    try:
      channel = self._ledger.idle.pop()
    except IndexError:
      return None
    with self._ledger.lock:
      self._ledger.in_flight += 1
    return channel

  def _best_live_throughput(self) -> float | None:
    """Returns the best throughput to saturate against: the max of any live sample and the last
    completed wave's best. Folded together rather than used only as a fallback -- a reused idle
    transport always has a live EWMA, which would otherwise shadow the persisted baseline entirely and
    leave first-channel saturation detection unable to trigger against a prior wave's peak.
    """
    samples = [s.ewma_throughput for s in self._ledger.states.values() if s.ewma_throughput is not None]
    if self._ledger.last_wave_best_throughput is not None:
      samples.append(self._ledger.last_wave_best_throughput)
    if samples:
      return max(samples)
    return None

  def _pick_growth_target(self) -> TransportState | None:
    """Returns a `TransportState` under its channel cap to open a new channel on, or `None` if
    every live `Transport` is at cap, saturated, or dead (the caller should dial a new `Transport`
    instead).

    A dead `Transport` (server dropped it while one of its channels was still checked out) only gets
    untracked in `release()`, once that checked-out channel comes back -- nothing here proactively
    evicts it. Without the `is_active()` check, every acquire() in the meantime would keep selecting
    it back (it has the fewest channels, and `min()` below prefers that), fail opening a channel on
    it, and roll back, instead of falling through to a live Transport or a fresh dial.
    """
    best = self._best_live_throughput()
    candidates = [
      s
      for s in self._ledger.states.values()
      if s.transport.is_active()
      and s.channel_count < (self.channels_per_transport if s.channel_cap is None else s.channel_cap)
      and not (best is not None and s.is_saturated(best))
    ]
    if not candidates:
      return None
    return min(candidates, key=lambda s: s.channel_count)

  def _reserve_on_existing(self) -> TransportState | None:
    """Claims a channel slot on an under-cap `Transport`, if there is one.

    Reading (which `Transport` is under-cap) and committing (claiming a slot on it) share one
    critical section: otherwise concurrent `acquire()` calls could all see the same under-cap
    `Transport` before any of them reserved, and all open a channel on it, overshooting
    `channels_per_transport`. The lock is dropped before returning -- callers go on to make real
    network calls that must not run while holding it.

    Returns:
      The chosen `TransportState`, its `channel_count` already incremented, or `None` if every live
      `Transport` is at cap or saturated.
    """
    with self._ledger.lock:
      target = self._pick_growth_target()
      if target is not None:
        target.channel_count += 1
      return target

  def _release_or_pop_saturated(self, channel: Channel) -> bool:
    """Returns a checked-out channel to the pool, or pops it if its `Transport` is saturated or the
    pool has been torn down.

    Args:
      channel: The channel being released.

    Returns:
      `True` if the channel was popped (caller must close the handle; `channel_count` has already
      been decremented here), `False` if it was returned to `ledger.idle` for reuse.

    A pop that empties the `Transport` closes it only when the pool is already torn down. Saturation
    leaves it open with its throughput samples cleared -- see the inline reasoning below.
    """
    best = self._best_live_throughput()
    emptied = False
    emptied_for_reuse = False
    with self._ledger.lock:
      # A sibling channel on the same Transport can have already called _drop_transport() (its own
      # fatal release, having found the Transport dead) between this method computing `best` above
      # and taking the lock here -- that sweeps ledger.idle for the Transport's state at that moment,
      # but can't see this channel, which hadn't been idled yet. Checking the state is still
      # registered catches that: without it, this would append an orphaned handle to ledger.idle
      # whose Transport nothing will ever sweep again, retained indefinitely if no later checkout
      # happens to pop and validate it.
      transport_dropped_by_sibling = id(channel.state.transport) not in self._ledger.states
      # A torn-down pool never checks ledger.idle again -- acquire() rejects outright via
      # raise_if_closed(), and the keepalive thread that would otherwise drain it is already
      # stopped by the time teardown() runs (PooledAdapterBase._shutdown_teardown). Idling a handle
      # here instead of closing it would leak the Transport (and its live paramiko background
      # thread) for the rest of the process's life.
      pool_closed = self._wakeup.is_closed()
      if transport_dropped_by_sibling or (best is not None and channel.state.is_saturated(best)) or pool_closed:
        channel.state.channel_count -= 1
        self._ledger.handle_states.pop(id(channel.handle), None)
        popped = True
        # Skipped entirely when the sibling already dropped it -- that sibling's own release() already
        # popped ledger.states and will account the slot via transport_dropped(); doing either again
        # here would double-count.
        now_empty = not transport_dropped_by_sibling and channel.state.channel_count == 0
        if now_empty and pool_closed:
          # Nothing will ever reserve on it again, so there is no reuse window worth preserving.
          emptied = self._ledger.states.pop(id(channel.state.transport), None) is not None
        elif now_empty:
          # Saturation emptied it. Closing here costs a full SSH handshake the moment the next
          # acquire needs a channel, and the replacement starts unmeasured -- so a pool whose
          # Transports differ at all settles into dialing a fresh one every few acquires, which is
          # exactly what this pool exists to avoid. What actually traps an emptied Transport is its
          # stale, low EWMA: with no channel left to refresh it, it would keep losing
          # _pick_growth_target()'s saturation filter forever while still holding a _current_size
          # slot. Clearing the samples releases it from that filter (no samples means not saturated)
          # so it can be picked again and re-measured on live traffic, and _EMPTY_TRANSPORT_TTL
          # still reaps it if nothing ever does. Reset before _mark_returned() below, so the
          # discarded measurement doesn't feed this wave's max either.
          channel.state.ewma_throughput = None
          channel.state.sample_count = 0
          channel.state.last_released = monotonic()
          emptied_for_reuse = True
      else:
        self._ledger.idle.append(channel)
        popped = False
    if emptied:
      self._connector.close_transport_handler(channel.state.transport)
      self._ledger.transports.transport_dropped()
    elif emptied_for_reuse:
      self._ensure_pruner_running()
    self._wakeup.signal()  # a slot freed up either way -- saturated pop or a fresh idle channel
    self._mark_returned(channel.state)
    return popped

  def _mark_returned(self, state: TransportState) -> None:
    """Records a channel as no longer in-flight, folding its throughput into the current wave's max.

    Once every in-flight channel has returned, snapshots the wave's max into
    `ledger.last_wave_best_throughput` and resets for the next wave.

    Args:
      state: The `TransportState` of the channel that just returned.
    """
    with self._ledger.lock:
      if state.ewma_throughput is not None:
        self._ledger.wave_running_max = max(self._ledger.wave_running_max, state.ewma_throughput)
      self._ledger.in_flight -= 1
      if self._ledger.in_flight == 0:
        self._ledger.last_wave_best_throughput = self._ledger.wave_running_max
        self._ledger.wave_running_max = 0.0

  def _discard(self, channel: Channel) -> None:
    """Stops tracking a channel (removing it from `ledger.idle` if present) and closes its handle.

    If this empties the Transport and the pool is still open, it's left registered rather than
    closed immediately -- one channel individually failing validation doesn't mean the Transport is
    unusable, and a caller reserving a new channel on it moments later is the common case this pool
    exists to serve; `_ensure_pruner_running` bounds how long it's allowed to sit open with nothing
    on it. A torn-down pool skips that grace period and drops it right away instead -- nothing will
    ever reserve on it again, so there's no reuse window worth waiting for, and starting a pruner
    thread just to sleep out the full TTL on an already-dead pool would be pure waste.

    Args:
      channel: The channel to discard.
    """
    dropped_now = False
    with self._ledger.lock:
      channel.state.channel_count -= 1
      channel.state.last_released = monotonic()
      self._ledger.handle_states.pop(id(channel.handle), None)
      if channel in self._ledger.idle:
        self._ledger.idle.remove(channel)
      emptied = channel.state.channel_count == 0
      if emptied and self._wakeup.is_closed():
        dropped_now = self._ledger.states.pop(id(channel.state.transport), None) is not None
    self._mark_returned(channel.state)
    self._connector.close_conn_handler(channel.handle)
    if dropped_now:
      self._connector.close_transport_handler(channel.state.transport)
      self._ledger.transports.transport_dropped()
    elif emptied:
      self._ensure_pruner_running()
    self._wakeup.signal()  # channel_count just dropped -- a blocked waiter may now be able to grow

  def _ensure_pruner_running(self) -> None:
    """Starts the single background prune thread if one isn't already running; a no-op otherwise.

    One thread services every empty Transport rather than one timer per empty event, so a pool that
    cycles through many short-lived channels doesn't spin up a thread per cycle. Also a no-op if the
    pool has been closed since the caller's own check -- `_discard` already skips calling this once
    `is_closed()`, but the two checks aren't atomic with each other, so this one closes that gap:
    without it, a thread could still start moments after `close()`, only to find `is_closed()` true
    on its very first loop and immediately retire having pruned nothing.
    """
    with self._prune_lock:
      if self._prune_thread is not None or self._wakeup.is_closed():
        return
      self._prune_thread = Thread(target=self._prune_loop, name="aeth-ext-sftp-transport-pruner", daemon=True)
      self._prune_thread.start()

  def _prune_loop(self) -> None:
    """Waits until the oldest currently-empty Transport reaches `_EMPTY_TRANSPORT_TTL` seconds idle,
    closes every Transport that's aged out by then, and repeats until none are left empty, at which
    point this thread retires -- `_ensure_pruner_running` starts a fresh one for the next empty spell.

    Wakes early on `_shutdown_wakeup` rather than sleeping out the rest of the TTL during a real
    process shutdown -- retires immediately on that signal with no further check, trusting it rather
    than re-verifying anything: whatever's left empty at that point is process teardown's problem to
    close, not this thread's. Not wired to any single pool's own close() -- SHUTDOWN_WAKEUP is process-
    wide, so a pool torn down some other way (a bare WakeupGate.close() in a test, say) doesn't wake
    this early; it simply prunes on its normal schedule instead, which is fine outside of shutdown.
    """
    while True:
      with self._ledger.lock:
        idle_states = [s for s in self._ledger.states.values() if s.channel_count == 0]
      if idle_states:
        oldest = min(idle_states, key=lambda s: s.last_released)
        wait = oldest.last_released + self._EMPTY_TRANSPORT_TTL - monotonic()
        if mp_connection.wait([self._shutdown_wakeup], timeout=max(wait, 0)):
          with self._prune_lock:
            self._prune_thread = None
          return
        with self._ledger.lock:
          now = monotonic()
          expired = [
            s
            for s in self._ledger.states.values()
            if s.channel_count == 0 and now - s.last_released >= self._EMPTY_TRANSPORT_TTL
          ]
          for s in expired:
            self._ledger.states.pop(id(s.transport), None)
        for s in expired:
          self._connector.close_transport_handler(s.transport)
          self._ledger.transports.transport_dropped()
        if expired:
          self._wakeup.signal()  # freed one or more _current_size slots
        continue

      # Nothing left to wait on right now -- but a fresh empty Transport could appear between the
      # check above and committing to exit. Re-check inside the same _prune_lock critical section
      # _ensure_pruner_running() also uses, so that method either sees this thread still owns the
      # "running" slot (and safely assumes it will loop back around, which the re-check guarantees)
      # or sees it cleared (and safely starts a new one) -- never a window where both think the other
      # is responsible and the newly-emptied Transport never gets pruned.
      with self._prune_lock, self._ledger.lock:
        if any(s.channel_count == 0 for s in self._ledger.states.values()):
          continue
        self._prune_thread = None
        return

  def _drop_transport(self, state: TransportState) -> tuple[bool, list[Channel]]:
    """Stops tracking a `Transport`, returning whichever of its channels were sitting idle.

    Channels still checked out elsewhere are not returned here -- they aren't tracked in `ledger.idle`
    and fail naturally on their next I/O (see the multiplexing design's Error handling section).

    Args:
      state: The `TransportState` to stop tracking.

    Returns:
      Whether this call was the one that actually removed `state` from `ledger.states` -- concurrent
      callers racing to drop the same dying `Transport` (one per checked-out sibling channel) all reach
      this method, but only the first should count against `_current_size` -- plus whichever of its
      channels were sitting idle.
    """
    with self._ledger.lock:
      was_first_to_drop = self._ledger.states.pop(id(state.transport), None) is not None
      all_idle = self._ledger.idle.copy()
      self._ledger.idle.clear()
      orphaned: list[Channel] = []
      for c in all_idle:
        if c.state is state:
          orphaned.append(c)
          self._ledger.handle_states.pop(id(c.handle), None)
        else:
          self._ledger.idle.append(c)
      return was_first_to_drop, orphaned

  def _validate(self, handle: SFTPClient) -> bool:
    """Checks whether a handle responds to a `listdir(".")` round trip.

    Args:
      handle: The handle to check.

    Returns:
      `True` if `handle` is still usable.
    """
    try:
      handle.listdir(".")
      return True
    except OSError as exc:
      # paramiko maps SFTP_PERMISSION_DENIED/SFTP_NO_SUCH_FILE status replies to IOError with
      # these errnos -- a non-listable root still means the server answered, so the channel is
      # fine. Any other OSError (socket reset, broken pipe, ...) means it isn't.
      return exc.errno in (errno.EACCES, errno.ENOENT)
    except SSHException, EOFError:
      # SSHException: protocol/transport failure. EOFError: listdir_attr() already swallows the
      # normal end-of-listing EOF internally, so one reaching here means the channel closed
      # mid-request. Either way the connection is unusable.
      return False

  def _make_instrument(self, state: TransportState) -> InstrumentCallable:
    """Builds a per-checkout observer callback that feeds elapsed-time-weighted throughput samples
    into a `TransportState`.

    Args:
      state: The `TransportState` to feed throughput samples into.

    Returns:
      The observer callback.
    """
    last_sample: float | None = None

    def observer(data: SizedBuffer) -> None:
      """Records the bytes transferred since the last call as a throughput sample.

      Args:
        data: The chunk just transferred.
      """
      nonlocal last_sample
      now = monotonic()
      if last_sample is None:
        # The first callback only establishes the timing baseline. Timing from checkout instead would
        # charge however long the session held the handle before its first chunk to transfer time --
        # and since the first sample *initializes* the EWMA outright and saturation activates after
        # only _MIN_SAMPLES, that idle delay alone can mark a healthy Transport saturated and dial a
        # needless extra one.
        last_sample = now
        return
      with self._ledger.lock:
        state.update_throughput(len(data), now - last_sample)
        # Folded in per sample, not just once at release: the EWMA tracks recent rate, so a transfer
        # that peaks mid-stream and tails off ends lower than it ran. _mark_returned() only sees that
        # final value, losing the peak entirely -- and the wave's max is what the *next* wave uses as
        # its saturation baseline, so an understated peak makes the next wave call healthy Transports
        # saturated and grow past them unnecessarily.
        if state.ewma_throughput is not None:
          self._ledger.wave_running_max = max(self._ledger.wave_running_max, state.ewma_throughput)
      last_sample = now

    return observer
