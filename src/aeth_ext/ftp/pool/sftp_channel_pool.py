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
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

# Third party imports
from paramiko import SSHException

# First party imports
from aeth_ext.ftp.pool.base import WakeupGate

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence
  from typing import Any

  # Third party imports
  from paramiko import SFTPClient, Transport

  # First party imports
  from aeth_ext.ftp.connectors import SFTPConnector
  from aeth_ext.ftp.pool.base import TransportDialer
  from aeth_ext.ftp.types import IntrumentCallable
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
  ewma_throughput: float | None = None
  sample_count: int = 0

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

  @_mirror_builtin(dict.pop)
  def pop(self, key: K, default: V | None = None) -> V | None:
    with self._lock:
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
  `HandleProvider`."""

  def __init__(self, ledger: ChannelLedger, connector: SFTPConnector, channels_per_transport: int) -> None:
    """Initializes an empty pool bound to `ledger`'s state and `connector`'s connection-opening.

    Args:
      ledger: The shared bookkeeping this pool reads and writes.
      connector: Opens channels on an existing `Transport` and closes both channels and whole `Transport`s.
      channels_per_transport: Maximum channels to multiplex onto a single `Transport`.
    """
    self._ledger = ledger
    self._connector = connector
    self.channels_per_transport = channels_per_transport
    self._wakeup = WakeupGate()

  def acquire(self) -> tuple[SFTPClient, Sequence[IntrumentCallable]]:
    """Checks out an idle channel if one validates, else multiplexes a new channel onto an
    under-cap `Transport`, dials a brand new `Transport`, or (if the pool is fully saturated)
    blocks until a channel is released.

    Returns:
      The handle, plus a throughput-instrumentation observer callback for it.
    """
    channel = self._checkout_idle_or_grow()
    if channel is None:
      # A retry loop that only re-checked ledger.idle would strand this caller when capacity frees up
      # without ever producing an idle channel (e.g. a fatal release of a dead-transport's last live
      # sibling). retry_until() re-runs the whole idle-then-growth decision on every wakeup instead.
      channel = self._wakeup.retry_until(self._checkout_idle_or_grow)

    return channel.handle, (self._make_instrument(channel.state),)

  def _checkout_idle_or_grow(self) -> Channel | None:
    """Tries an idle channel (revalidating it), else multiplexes onto an under-cap `Transport` or
    dials a brand new one within the pool's ceiling.

    Returns:
      A usable channel, or `None` if none of the above is currently available.
    """
    channel = self._checkout_idle()
    if channel is not None and not self._validate(channel.handle):
      self._discard(channel)
      channel = None

    if channel is None:
      # Read (which transport is under-cap) and commit (reserve a slot on it) must happen in the same
      # critical section -- otherwise concurrent acquire() calls can all observe the same under-cap
      # transport before any of them reserves, and all open a channel on it, overshooting
      # channels_per_transport. Both open_transport() and request_handler() below are real network
      # calls, so neither can run while the lock is held.
      with self._ledger.lock:
        target = self._pick_growth_target()
        if target is not None:
          target.channel_count += 1
      if target is None:
        transport = self._ledger.transports.open_transport()
        if transport is not None:
          with self._ledger.lock:
            target = TransportState(transport=transport, channel_count=1)
            self._ledger.states[id(transport)] = target
      if target is not None:
        try:
          handle = self._connector.request_handler(target.transport)
        except Exception:
          # Roll back this reservation; tear the Transport down only if that left nothing on it.
          # Keying teardown off "did I dial this one" instead fails both ways: a sibling that
          # reserved a slot while this open was in flight would get its Transport closed underneath
          # it, and if that sibling's own open then failed too, the Transport would stay registered
          # at zero channels -- permanently failing every growth attempt that picks it, its slot
          # never returned. Decrement, zero-check and states.pop share one critical section, so
          # teardown fires exactly once: once the state is gone, no later _pick_growth_target() can
          # find it to reserve on, so no second rollback can reach zero again.
          with self._ledger.lock:
            target.channel_count -= 1
            abandoned = target.channel_count == 0 and self._ledger.states.pop(id(target.transport), None) is not None
          if abandoned:
            self._connector.close_transport_handler(target.transport)
            self._ledger.transports.transport_dropped()
          raise
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
    """Closes every tracked `Transport` (and every channel opened on it).

    Reports each closed `Transport` to `ledger.transports.transport_dropped()` -- without this, a
    reused pool (e.g. tests calling `_shutdown_teardown()` directly) would see `SFTPAdapter._current_size`
    stuck at its pre-teardown value, blocking new growth unnecessarily.
    """
    with self._ledger.lock:
      states = self._ledger.states.values()
      self._ledger.states.clear()
      self._ledger.handle_states.clear()
      self._ledger.idle.clear()
    for state in states:
      self._connector.close_transport_handler(state.transport)
      self._ledger.transports.transport_dropped()

  def keepalive_check_one(self) -> None:
    """Pops and validates one idle channel, discarding it (and possibly its `Transport`) if the
    check fails."""
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
    """Returns the best currently-tracked throughput, falling back to the last completed wave's best
    if no transport has a live sample yet."""
    samples = [s.ewma_throughput for s in self._ledger.states.values() if s.ewma_throughput is not None]
    if samples:
      return max(samples)
    return self._ledger.last_wave_best_throughput

  def _pick_growth_target(self) -> TransportState | None:
    """Returns a `TransportState` under its channel cap to open a new channel on, or `None` if
    every live `Transport` is at cap or saturated (the caller should dial a new `Transport` instead).
    """
    best = self._best_live_throughput()
    candidates = [
      s
      for s in self._ledger.states.values()
      if s.channel_count < self.channels_per_transport and not (best is not None and s.is_saturated(best))
    ]
    if not candidates:
      return None
    return min(candidates, key=lambda s: s.channel_count)

  def _release_or_pop_saturated(self, channel: Channel) -> bool:
    """Returns a checked-out channel to the pool, or pops it if its `Transport` is saturated.

    Args:
      channel: The channel being released.

    Returns:
      `True` if the channel was popped (its `Transport` is saturated -- caller must close the
      handle; `channel_count` has already been decremented here), `False` if it was returned to
      `ledger.idle` for reuse.
    """
    best = self._best_live_throughput()
    with self._ledger.lock:
      if best is not None and channel.state.is_saturated(best):
        channel.state.channel_count -= 1
        self._ledger.handle_states.pop(id(channel.handle), None)
        popped = True
      else:
        self._ledger.idle.append(channel)
        popped = False
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

    Args:
      channel: The channel to discard.
    """
    with self._ledger.lock:
      channel.state.channel_count -= 1
      self._ledger.handle_states.pop(id(channel.handle), None)
      if channel in self._ledger.idle:
        self._ledger.idle.remove(channel)
    self._mark_returned(channel.state)
    self._connector.close_conn_handler(channel.handle)
    self._wakeup.signal()  # channel_count just dropped -- a blocked waiter may now be able to grow

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

  def _make_instrument(self, state: TransportState) -> IntrumentCallable:
    """Builds a per-checkout observer callback that feeds elapsed-time-weighted throughput samples
    into a `TransportState`.

    Args:
      state: The `TransportState` to feed throughput samples into.

    Returns:
      The observer callback.
    """
    last_sample = monotonic()

    def observer(data: SizedBuffer) -> None:
      """Records the bytes transferred since the last call as a throughput sample.

      Args:
        data: The chunk just transferred.
      """
      nonlocal last_sample
      now = monotonic()
      with self._ledger.lock:
        state.update_throughput(len(data), now - last_sample)
      last_sample = now

    return observer
